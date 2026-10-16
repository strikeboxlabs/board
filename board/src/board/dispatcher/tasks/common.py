from __future__ import annotations

import logging
import time
import uuid

import yaml
from dataclasses import dataclass

from board.dispatcher.config import DispatchConfig, WorkerConfig
from board.dispatcher.protocol.client import BoardClient
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.runtime.containers import ContainerManager
from board.dispatcher.runtime.heartbeat import HeartbeatLease
from board.dispatcher.runtime.process import ProcessResult
from board.reporting.evidence import hash_file, infer_evidence_kind, walk_evidence_files

PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    if config.runtime.execution == "local":
        return False
    return config.runtime.worker_healthcheck == "startup_and_task"


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    project_id: str,
    graph_yaml: str,
    *,
    phase: str,
    source_fact_ids: list[str] | None = None,
) -> str:
    # Snapshots live in the project's scratch dir (durable root), not a hardcoded /tmp path.
    path = f"{container_manager.scratch_dir(project_id)}/graph-{phase}-{uuid.uuid4().hex[:12]}.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    if source_fact_ids is not None:
        focused = focus_graph_yaml(graph_yaml, source_fact_ids)
        focused_path = path.removesuffix(".yaml") + "-focus.yaml"
        container_manager.write_text_file(container_name, focused_path, focused)
        return ("Read the intent-focused dependency view first:\n" + focused_path
                + "\nThe complete graph remains available when needed:\n" + path
                + "\nThe focused view omits unrelated branches; it does not establish whole-project coverage.")
    return (
        "The complete graph YAML snapshot is stored in the worker environment:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def focus_graph_yaml(graph_yaml: str, source_fact_ids: list[str]) -> str:
    """Keep source facts and their provenance; retain the full snapshot separately."""
    graph = yaml.safe_load(graph_yaml)
    if not isinstance(graph, dict):
        return graph_yaml
    selected = {"origin", "goal", *source_fact_ids}
    intents = graph.get("intents", [])
    changed = True
    while changed:
        changed = False
        for intent in intents:
            if intent.get("to") in selected:
                parents = set(intent.get("from", []))
                if not parents.issubset(selected):
                    selected.update(parents)
                    changed = True
    focused = {key: value for key, value in graph.items() if key not in ("facts", "intents", "jobs")}
    facts = graph.get("facts", [])
    focused["facts"] = [fact for fact in facts if fact.get("id") in selected]
    focused["intents"] = [intent for intent in intents if intent.get("to") in selected]
    focused["jobs"] = [job for job in graph.get("jobs", []) if job.get("fact") in selected]
    focused["context_view"] = {"kind": "intent-dependencies", "source_fact_ids": source_fact_ids,
        "omitted_facts": len(facts) - len(focused["facts"]),
        "omitted_intents": len(intents) - len(focused["intents"])}
    return yaml.safe_dump(focused, allow_unicode=True, sort_keys=False)


def run_worker_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    env_extra: dict[str, str] | None = None,
) -> ProcessResult:
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    env = dict(worker.env)
    if env_extra:
        env.update(env_extra)
    process = container_manager.build_exec_process(
        container_name,
        env,
        argv,
        timeout_seconds=timeout_seconds,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    started = time.monotonic()
    try:
        result = process.communicate(timeout=communicate_timeout(timeout_seconds))
        project_id = (env_extra or {}).get("BOARD_PROJECT_ID")
        if project_id:
            try:
                from board.dispatcher.metrics import record_metric
                from board.dispatcher.workers.adapters.pi import PiDriver
                diagnostic = PiDriver.output_diagnostics(result.stdout) if worker.type == "pi" else {"usage_available": False}
                record_metric(container_manager, project_id, {
                    "event": "process", "phase": phase, "worker": worker.name,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "returncode": result.returncode, "timed_out": result.timed_out,
                    "cancelled": result.cancelled, **diagnostic,
                })
            except Exception:
                LOG.exception("could not collect process metrics project=%s", project_id)
        return result
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def register_intent_evidence(
    client: BoardClient,
    container_manager: ContainerManager,
    project_id: str,
    intent_id: str,
    fact_id: str | None = None,
) -> int:
    """Walk the intent's evidence dir, hash every artefact, register rows via the API.

    Best-effort: an evidence failure must never fail the task that produced the fact.
    """
    try:
        directory = container_manager.evidence_dir(project_id, intent_id)
        files = walk_evidence_files(directory)
    except Exception:
        LOG.exception(
            "evidence walk failed project=%s intent=%s", project_id, intent_id
        )
        return 0

    registered = 0
    for path in files:
        try:
            sha256, size_bytes = hash_file(path)
            stored_path = container_manager.evidence_relative_path(project_id, intent_id, path)
            response = client.register_evidence(
                project_id,
                {
                    "path": stored_path,
                    "kind": infer_evidence_kind(path.name),
                    "intent_id": intent_id,
                    "fact_id": fact_id,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                },
            )
            if response.ok:
                registered += 1
            else:
                LOG.warning(
                    "evidence registration rejected project=%s intent=%s path=%s status=%s body=%s",
                    project_id,
                    intent_id,
                    stored_path,
                    response.status_code,
                    response.text,
                )
        except Exception:
            LOG.exception(
                "evidence registration failed project=%s intent=%s path=%s",
                project_id,
                intent_id,
                path,
            )
    if files:
        LOG.info(
            "intent evidence registered project=%s intent=%s files=%s ok=%s fact=%s",
            project_id,
            intent_id,
            len(files),
            registered,
            fact_id,
        )
    return registered


def insert_worker_findings(
    client: BoardClient,
    project_id: str,
    intent_id: str,
    fact_id: str | None,
    findings: list[dict],
) -> int:
    """Insert structured findings a worker returned alongside its fact description."""
    if not findings:
        return 0
    if fact_id is None:
        LOG.warning(
            "worker findings dropped because fact id is unknown project=%s intent=%s count=%s",
            project_id,
            intent_id,
            len(findings),
        )
        return 0
    created = 0
    for finding in findings:
        payload = {**finding, "fact_id": fact_id, "intent_id": intent_id}
        response = client.create_finding(project_id, payload)
        if response.ok:
            created += 1
        else:
            LOG.warning(
                "finding insert failed project=%s intent=%s fact=%s status=%s body=%s",
                project_id,
                intent_id,
                fact_id,
                response.status_code,
                response.text,
            )
    LOG.info(
        "worker findings recorded project=%s intent=%s fact=%s created=%s/%s",
        project_id,
        intent_id,
        fact_id,
        created,
        len(findings),
    )
    return created


def persist_project_exports(
    client: BoardClient,
    container_manager: ContainerManager,
    project_id: str,
) -> bool:
    """Refresh <project>/reports/timeline.txt and graph.yaml.

    The server renders both (single source of truth); the dispatcher persists them so
    the timeline never again exists only inside the UX.
    """
    try:
        reports_dir = container_manager.reports_dir(project_id)
        for filename, export_format in (("timeline.txt", "timeline"), ("graph.yaml", "yaml")):
            text = client.export_project(project_id, export_format)
            (reports_dir / filename).write_text(text, encoding="utf-8")
        return True
    except Exception:
        LOG.warning("failed to persist project exports project=%s", project_id, exc_info=True)
        return False


def persist_project_reports(
    client: BoardClient,
    container_manager: ContainerManager,
    project_id: str,
) -> bool:
    """Persist the full report set on project completion (no human action required)."""
    exports_ok = persist_project_exports(client, container_manager, project_id)
    rendered = []
    try:
        reports_dir = container_manager.reports_dir(project_id)
        fetches = [
            ("agent", "report-agent.md"),
            ("agent-json", "report-agent.json"),
            ("human", "report-human.md"),
        ]
        for report_type, filename in fetches:
            response = client.get_report(project_id, report_type)
            if response.ok:
                (reports_dir / filename).write_text(response.text, encoding="utf-8")
                rendered.append(filename)
            else:
                LOG.warning(
                    "report fetch failed project=%s type=%s status=%s",
                    project_id,
                    report_type,
                    response.status_code,
                )
        for asset_format, filename in (
            ("json", "asset-map.json"),
            ("md", "asset-inventory.md"),
            ("html", "asset-map.html"),
        ):
            response = client.get_asset_map(project_id, asset_format)
            if response.ok:
                (reports_dir / filename).write_text(response.text, encoding="utf-8")
                rendered.append(filename)
            else:
                LOG.warning(
                    "asset map fetch failed project=%s format=%s status=%s",
                    project_id,
                    asset_format,
                    response.status_code,
                )
    except Exception:
        LOG.warning("failed to persist project reports project=%s", project_id, exc_info=True)
    if rendered:
        LOG.info("project reports persisted project=%s files=%s", project_id, rendered)
    return exports_ok and len(rendered) == 6


def project_allows_conclude_fallback(client: BoardClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: BoardClient, project_id: str, worker_name: str, lease_id: str | None = None) -> None:
    response = client.release_reason(project_id, worker_name, lease_id) if lease_id is not None else client.release_reason(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def write_conclude_result(
    client: BoardClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    ).status


def write_conclude_result_with_fact_id(
    client: BoardClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> ConcludeWriteResult:
    response = client.conclude(project_id, intent_id, worker_name, description)
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code == 403:
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


class TaskFailure(str):
    """String-compatible task outcome carrying a diagnostic for the dispatcher."""
    def __new__(cls, outcome: str, error: str):
        result = super().__new__(cls, outcome)
        result.error = error
        return result


def best_effort_release(client: BoardClient, project_id: str, intent_id: str, worker_name: str) -> None:
    response = client.release(project_id, intent_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
