from __future__ import annotations

import logging
import json
import uuid
import time

from board.dispatcher.skills import available_skill_profiles, prepare_pi_worker, profile_summaries
from board.dispatcher.config import DispatchConfig, WorkerConfig
from board.dispatcher.contracts import UnavailableSkillProfileError, parse_json_output, validate_reason_payload
from board.dispatcher.prompting import (
    with_execution_constraints,
    format_fact_ids,
    format_open_intents,
    load_prompt,
    render_prompt,
)
from board.dispatcher.protocol.client import BoardClient
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.runtime.containers import ContainerManager
from board.dispatcher.runtime.heartbeat import HeartbeatLease
from board.dispatcher.tasks.common import (
    best_effort_release_reason,
    cancel_reason,
    did_timeout,
    persist_project_reports,
    preview,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from board.dispatcher.workers.registry import get_driver
from board.server.models import ProjectDetail
from board.planning import planner_input_signature

LOG = logging.getLogger(__name__)


def run_reason_task(
    config: DispatchConfig,
    client: BoardClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type, config.runtime.execution)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease_id = project.project.reason.lease_id if project.project.reason else None
    lease = HeartbeatLease.for_reason(client, project.project.id, worker.name, config.runtime.interval, lease_id)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.id)
        worker = prepare_pi_worker(config, worker, container_manager, project.project.id, planning=True)

        if task_healthcheck_enabled(config):
            LOG.info(
                "checking worker health project=%s worker=%s timeout=%ss",
                project.project.id,
                worker.name,
                healthcheck_timeout,
            )
            health = driver.check_health(worker, timeout=healthcheck_timeout)
            if cancellation.is_cancelled:
                LOG.info(
                    "reason cancelled during healthcheck project=%s worker=%s reason=%s",
                    project.project.id,
                    worker.name,
                    cancellation.reason,
                )
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during reason healthcheck project=%s worker=%s status=%s",
                    project.project.id,
                    worker.name,
                    lease.failure.status_code,
                )
                return "failed"
            if not health.ok:
                LOG.warning(
                    "worker unhealthy project=%s worker=%s status=%s detail=%s",
                    project.project.id,
                    worker.name,
                    health.status,
                    health.detail,
                )
                return "unhealthy"
        open_intents = [
            {
                "id": intent.id,
                "from": intent.from_,
                "description": intent.description,
                "worker": intent.worker,
                "priority": intent.priority,
                "blocked_by": intent.blocked_by,
                "assigned_worker": intent.assigned_worker,
                "assigned_worker_type": intent.assigned_worker_type,
                "skill_profiles": intent.skill_profiles,
                "dispatch_error": intent.dispatch_error,
                "cancel_requested_at": intent.cancel_requested_at,
            }
            for intent in project.intents
            if intent.to is None and not intent.excluded
        ]
        allowed_fact_ids = [fact.id for fact in project.facts if fact.id != "goal"]
        LOG.debug(
            "reason context prepared project=%s worker=%s facts=%s allowed_fact_ids=%s hints=%s open_intents=%s",
            project.project.id,
            worker.name,
            len(project.facts),
            len(allowed_fact_ids),
            len(project.hints),
            len(open_intents),
        )
        capacity = max(0, config.tasks.reason.target_open_intents - len(open_intents))
        max_intents = min(config.tasks.reason.max_intents, capacity)
        available_profiles = available_skill_profiles(config.skills)
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "reason.md"),
            {
                "graph_yaml": export_yaml.strip() if worker.type == "pi" else write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    project.project.id,
                    export_yaml.strip(),
                    phase="reason_execute",
                ),
                "fact_ids": format_fact_ids(allowed_fact_ids),
                "open_intents": format_open_intents(open_intents),
                "max_intents": str(max_intents),
                "skill_profiles": profile_summaries(config.skills, available_profiles),
            },
        )

        prompt = with_execution_constraints(prompt, project)
        session = driver.prepare_session()
        if worker.type == "pi":
            # Pass the complete graph as a file attachment: one bounded model
            # request, no graph-reading tool turns or command-line size limit.
            nonce = uuid.uuid4().hex[:12]
            prompt_path = f"{container_manager.scratch_dir(project.project.id)}/planner-{nonce}.md"
            extension_path = f"{container_manager.scratch_dir(project.project.id)}/planner-{nonce}.mjs"
            container_manager.write_text_file(container_name, prompt_path, prompt)
            # Pi's custom model lacks reasoning metadata, so --thinking alone
            # cannot set the OpenAI-compatible endpoint's reasoning effort.
            extension = """export default function (pi) {
  pi.on('before_provider_request', (event, ctx) => {
    const p = { ...event.payload };
    if (ctx.model?.api === 'openai-completions') {
      const key = 'max_completion_tokens' in p ? 'max_completion_tokens' : 'max_tokens';
      p[key] = MAX_OUTPUT;
      p.reasoning_effort = EFFORT;
    }
    return p;
  });
}
""".replace("MAX_OUTPUT", str(config.tasks.reason.pi_max_output_tokens)).replace(
                "EFFORT", json.dumps(config.tasks.reason.pi_reasoning_effort))
            container_manager.write_text_file(container_name, extension_path, extension)
            command = driver.build_reason(worker, prompt_path, extension_path)
        else:
            command = driver.build_execute(worker, prompt, session)
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="reason_execute",
            timeout_seconds=config.tasks.reason.timeout,
            lease=lease,
            cancellation=cancellation,
            env_extra=container_manager.worker_env(project.project.id),
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "reason cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during reason project=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "reason timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "reason command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            kind, data = validate_reason_payload(
                payload, open_intents_empty=not open_intents, max_intents=max_intents,
                allowed_skill_profiles=available_profiles.keys(),
            )
        except UnavailableSkillProfileError as exc:
            # IDs have already passed the bounded identifier syntax checks. Keep
            # the diagnostic useful without dumping the raw model response.
            LOG.warning("reason profile validation failed project=%s worker=%s error=%s", project.project.id, worker.name, exc)
            return "failed"
        except Exception as exc:
            LOG.warning(
                "reason parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if kind == "wait":
            LOG.info("reason deferred project=%s explanation=%s", project.project.id, data["reason"])
            from board.dispatcher.metrics import record_metric
            record_metric(container_manager, project.project.id, {"event": "planner_wait", "explanation": data["reason"]})
            if not lease_id:
                LOG.warning("cannot persist planner wait without a lease token project=%s", project.project.id)
                return "failed"
            response = client.record_reason_wait(project.project.id, worker.name, lease_id,
                                                 data["reason"], planner_input_signature(project))
            if not response.ok and response.status_code not in (403, 409):
                LOG.warning("planner wait write failed project=%s status=%s", project.project.id, response.status_code)
                return "failed"
            return "success"
        if kind == "rejected":
            LOG.warning(
                "reason rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return "rejected"
        if kind == "complete":
            response = client.complete(project.project.id, data["from"], data["description"], worker.name)
            if response.status_code == 403:
                LOG.info("project became inactive during reason complete project=%s worker=%s", project.project.id, worker.name)
                return "success"
            if not response.ok:
                LOG.warning(
                    "reason complete write failed project=%s worker=%s status=%s body=%s",
                    project.project.id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                return "failed"
            LOG.info(
                "project completed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                data["from"],
                execute_ms,
                total_ms,
            )
            persist_project_reports(client, container_manager, project.project.id)
            return "success"
        if kind == "intents":
            created = 0
            # A human can add intents while planning runs. Recheck capacity and
            # exact duplicates immediately before writing the proposed batch.
            current = client.get_project(project.project.id)
            current_open = [intent for intent in current.intents if intent.to is None and not intent.excluded]
            available = max(0, config.tasks.reason.target_open_intents - len(current_open))
            existing = {(tuple(sorted(intent.from_)), intent.description.strip().casefold()) for intent in current.intents if intent.to is None}
            if available == 0:
                return "success"
            for intent_data in data:
                if created >= available:
                    break
                key = (tuple(sorted(intent_data["from"])), intent_data["description"].strip().casefold())
                if key in existing:
                    continue
                existing.add(key)
                response = client.create_intent(
                    project.project.id,
                    intent_data["from"],
                    intent_data["description"],
                    worker.name,
                    priority=intent_data.get("priority"),
                    blocked_by=intent_data.get("blocked_by"),
                    **({"skill_profiles": intent_data["skill_profiles"]} if "skill_profiles" in intent_data else {}),
                )
                if response.status_code == 403:
                    LOG.info("project became inactive during reason intent create project=%s worker=%s created=%s", project.project.id, worker.name, created)
                    return "success"
                if response.status_code == 409:
                    LOG.info("reason intent lost race project=%s worker=%s from=%s", project.project.id, worker.name, intent_data["from"])
                    continue
                if not response.ok:
                    LOG.warning(
                        "reason intent write failed project=%s worker=%s status=%s body=%s",
                        project.project.id,
                        worker.name,
                        response.status_code,
                        response.text,
                    )
                    continue
                created += 1
                LOG.info(
                    "reason created intent project=%s worker=%s from=%s description=%s",
                    project.project.id,
                    worker.name,
                    intent_data["from"],
                    intent_data["description"],
                )
            LOG.info(
                "reason finished project=%s worker=%s created_intents=%s/%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                created,
                len(data),
                execute_ms,
                total_ms,
            )
            if created == 0:
                LOG.warning(
                    "reason created no intents project=%s worker=%s attempted=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    worker.name,
                    len(data),
                    execute_ms,
                    total_ms,
                )
                return "failed"
            return "success"
        LOG.info(
            "reason finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
            project.project.id,
            worker.name,
            execute_ms,
            total_ms,
        )
        return "success"
    finally:
        lease.stop()
        best_effort_release_reason(client, project.project.id, worker.name, lease_id)
