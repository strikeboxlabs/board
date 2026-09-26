from __future__ import annotations

import logging
import shutil
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from board.dispatcher.config import DispatchConfig, LocalConfig, WorkerConfig
from board.dispatcher.models import ReasonCheckpoint, RunningTask
from board.dispatcher.protocol.client import BoardClient
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.runtime.containers import ContainerManager
from board.dispatcher.runtime.local_backend import LocalBackend
from board.dispatcher.runtime.startup_healthcheck import format_failure_summary, run_startup_healthchecks
from board.dispatcher.scheduler.worker_select import choose_worker
from board.dispatcher.scheduler.reason_gate import ReasonGate, graph_signature
from board.dispatcher.metrics import record_metric
from board.dispatcher.workers.registry import get_driver
from board.dispatcher.tasks.bootstrap import run_bootstrap_task
from board.dispatcher.tasks.common import persist_project_reports
from board.dispatcher.tasks.explore import run_explore_task
from board.dispatcher.tasks.reason import run_reason_task
from board.server.models import Intent, ProjectDetail, ProjectSummary

LOG = logging.getLogger(__name__)
UNHEALTHY_RETRY_AFTER_SECONDS = 5
REJECTED_RETRY_AFTER_SECONDS = 5
BOOTSTRAP_INTENT_DESCRIPTION = "bootstrap"
BOOTSTRAP_INTENT_CREATOR = "dispatcher.bootstrap"
JOBS_CHECK_TIMEOUT_SECONDS = 20
JOBS_RESULT_MAX_CHARS = 2000
JOBS_PROGRESS_MAX_CHARS = 400
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(slots=True)
class WorkerSelection:
    worker: WorkerConfig | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_type: list[str]


class DispatcherLoop:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = DispatchConfig.load(config_path)
        self.client = BoardClient(self.config.server)
        if self.config.runtime.execution == "local":
            self.container_manager = LocalBackend(self.config.local or LocalConfig())
        else:
            assert self.config.container is not None
            self.container_manager = ContainerManager(self.config.container)
        self.reason_gate = ReasonGate(self.container_manager, self.config.tasks.reason)
        self.executor = ThreadPoolExecutor(max_workers=self.config.runtime.max_workers)
        self.cleanup_executor = ThreadPoolExecutor(max_workers=max(1, min(8, self.config.runtime.max_workers)))
        self.futures: dict[Future[str], RunningTask] = {}
        self.cleanup_futures: dict[Future[bool], tuple[str, str | None, str | None]] = {}
        self.reason_checkpoints: dict[str, ReasonCheckpoint] = {}
        self.runtime_project_ids: set[str] = set()
        self.worker_unhealthy_until: dict[str, float] = {}
        self.worker_rejected_until: dict[tuple[str, str, str], float] = {}
        self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
        self._cleanup_pending: set[str] = set()
        self._inactive_cleanup_done: dict[str, str] = {}
        self._abort_reports_done: set[str] = set()
        self.project_cursor = 0
        self._settings_checked = False
        self._startup_healthchecks_checked = False
        self._job_checks_inflight: set[tuple[str, str]] = set()
        self._job_check_futures: dict[Future, tuple[str, str]] = {}
        self._job_stop_futures = {}
        self._job_stop_results = {}

    def close(self) -> None:
        if self.futures:
            LOG.info(
                "dispatcher shutting down waiting_for_tasks=%s running_projects=%s",
                len(self.futures),
                sorted({task.project_id for task in self.futures.values()}),
            )
        self.executor.shutdown(wait=True)
        self.cleanup_executor.shutdown(wait=True)
        self.container_manager.close()
        self.client.close()

    def run(self, once: bool = False) -> None:
        try:
            self.run_startup_healthchecks()
            while True:
                try:
                    if not self._settings_checked:
                        self._validate_server_settings()
                        self._settings_checked = True
                    self._reap_futures()
                    self._reap_cleanup_futures()
                    summaries = self.client.list_projects()
                    self._initialize_reason_checkpoints(summaries)
                    self._refresh_runtime_projects(summaries)
                    self._cancel_inactive_tasks(summaries)
                    self._queue_container_cleanups(summaries)
                    self._persist_aborted_reports(summaries)
                    self._monitor_jobs(summaries)
                    self._acknowledge_suspensions(summaries)
                    self._dispatch_available(summaries)
                except requests.RequestException as exc:
                    if once:
                        raise
                    LOG.warning(
                        "dispatcher server request failed error=%s retry_in=%ss",
                        exc,
                        self.config.runtime.interval,
                    )
                    time.sleep(self.config.runtime.interval)
                    continue
                if once:
                    break
                time.sleep(self.config.runtime.interval)
        finally:
            self.close()

    def run_startup_healthchecks_only(self) -> None:
        try:
            self.run_startup_healthchecks(show_commands=True, force=True)
        finally:
            self.close()

    def run_startup_healthchecks(self, *, show_commands: bool = False, force: bool = False) -> None:
        if self._startup_healthchecks_checked:
            return
        if self.config.runtime.execution == "local":
            self._run_local_binary_check()
            self._startup_healthchecks_checked = True
            return
        if not force and self.config.runtime.worker_healthcheck == "disabled":
            LOG.info("skip startup worker healthchecks because runtime.worker_healthcheck=disabled")
            self._startup_healthchecks_checked = True
            return
        self._run_startup_healthchecks(show_commands=show_commands)
        self._startup_healthchecks_checked = True

    def _run_local_binary_check(self) -> None:
        binaries: dict[str, list[str]] = {}
        for worker in self.config.workers:
            binary = get_driver(worker.type, "local").local_binary()
            if binary is None:
                continue
            binaries.setdefault(binary, []).append(worker.name)
        if not binaries:
            return

        LOG.info("[*] Local execution: checking %d worker CLI(s) on this host", len(binaries))
        available: list[str] = []
        missing: list[str] = []
        for binary in sorted(binaries):
            workers = ", ".join(sorted(binaries[binary]))
            path, runnable = self._probe_local_cli(binary)
            if path is None:
                missing.append(binary)
                LOG.error("[-] %-8s not found on PATH (workers: %s)", binary, workers)
            elif runnable:
                available.append(binary)
                LOG.info("[+] %-8s %s (workers: %s)", binary, path, workers)
            else:
                available.append(binary)
                LOG.warning("[!] %-8s %s found but `%s --help` failed (workers: %s)", binary, path, binary, workers)

        if not available:
            raise RuntimeError(
                "local execution: none of the configured worker CLIs are installed on PATH ("
                + ", ".join(sorted(binaries))
                + "). Install them and make sure each runs directly from your shell, then retry."
            )
        if missing:
            LOG.warning(
                "[!] Missing CLIs, their workers cannot run: %s. Install them or drop those workers.",
                ", ".join(sorted(missing)),
            )
        LOG.warning(
            "[!] Local mode uses each CLI's own host config: make sure %s already logged in / "
            "configured and usable directly (e.g. `claude -p ...` works) — Strikebox injects no API keys.",
            ", ".join(sorted(available)),
        )

    @staticmethod
    def _probe_local_cli(binary: str) -> tuple[str | None, bool]:
        path = shutil.which(binary)
        if path is None:
            return None, False
        try:
            result = subprocess.run(
                [binary, "--help"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return path, False
        return path, result.returncode == 0

    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        if len(self.futures) >= self.config.runtime.max_workers:
            self._log_changed(
                "dispatch/global",
                logging.INFO,
                "skip dispatch because max_workers reached running_tasks=%s",
                len(self.futures),
            )
            return
        active = [summary for summary in summaries if summary.status == "active"]
        if not active:
            self._log_changed("dispatch/global", logging.INFO, "skip dispatch because no active projects")
            return

        running_projects = self._ordered_projects(
            [summary for summary in active if summary.id in self.runtime_project_ids]
        )
        idle_projects = self._ordered_projects(
            [summary for summary in active if summary.id not in self.runtime_project_ids]
        )

        dispatched = True
        while dispatched and len(self.futures) < self.config.runtime.max_workers:
            dispatched = False
            for summary in running_projects:
                if self._try_dispatch_project(summary):
                    dispatched = True
                    if len(self.futures) >= self.config.runtime.max_workers:
                        return
            if dispatched:
                continue
            if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                self._log_changed(
                    "dispatch/idle-limit",
                    logging.INFO,
                    "skip idle project dispatch because max_running_projects reached running_projects=%s",
                    self._running_project_count(active),
                )
                return
            for summary in idle_projects:
                if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                    self._log_changed(
                        "dispatch/idle-limit",
                        logging.INFO,
                        "stop idle project dispatch because max_running_projects reached running_projects=%s",
                        self._running_project_count(active),
                    )
                    return
                if self._try_dispatch_project(summary):
                    dispatched = True
                    break

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        if not summaries:
            return []
        ids = [summary.id for summary in summaries]
        ids.sort()
        offset = self.project_cursor % len(ids)
        ordered_ids = ids[offset:] + ids[:offset]
        by_id = {summary.id: summary for summary in summaries}
        self.project_cursor += 1
        return [by_id[project_id] for project_id in ordered_ids]

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        skip_scope = f"project:{summary.id}:skip"
        container_name = self.container_manager.container_name(summary.id)
        if container_name in self._cleanup_pending:
            self._log_changed(
                f"{skip_scope}:cleanup_pending",
                logging.DEBUG,
                "skip project=%s because container cleanup is still pending container=%s",
                summary.id,
                container_name,
            )
            return False
        if self._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            self._log_changed(
                f"{skip_scope}:max_project_workers",
                logging.INFO,
                "skip project=%s because max_project_workers reached running_tasks=%s",
                summary.id,
                self._project_running_task_summary(summary.id),
            )
            return False

        project = self.client.get_project(summary.id)
        if project.project.status != "active":
            self._log_changed(
                f"{skip_scope}:status",
                logging.INFO,
                "skip project=%s because status=%s",
                summary.id,
                project.project.status,
            )
            return False
        if self._is_initial_project(project):
            if project.project.reason is not None:
                return False
            if self._project_requires_bootstrap(project):
                return self._dispatch_initial_project(project)
            export_yaml = self.client.export_project(summary.id)
            return self._dispatch_reason(project, export_yaml, "initial")
        if project.project.reason is None:
            reason_trigger = self._reason_trigger(project)
            if reason_trigger is not None:
                export_yaml = self.client.export_project(summary.id)
                if self._dispatch_reason(project, export_yaml, reason_trigger):
                    return True
        running_intent_ids = self._project_running_explore_intents(summary.id)
        unclaimed_intents = [
            intent
            for intent in project.intents
            if intent.to is None
            and not intent.excluded
            and intent.worker is None
            and intent.id not in running_intent_ids
            and not self._is_bootstrap_intent(intent)
        ]
        if running_intent_ids and not unclaimed_intents:
            self._log_changed(
                f"{skip_scope}:explore_running",
                logging.DEBUG,
                "skip explore project=%s because all unclaimed intents are already running locally intents=%s",
                summary.id,
                sorted(running_intent_ids),
            )
        if unclaimed_intents:
            running_job_ids = {job.id for job in project.jobs if job.status == "running"}
            runnable_intents: list[Intent] = []
            pinned_waiting: list[str] = []
            job_blocked = 0
            for intent in unclaimed_intents:
                if intent.blocked_by and intent.blocked_by in running_job_ids:
                    job_blocked += 1
                    continue
                if intent.assigned_worker and not self._worker_available(
                    summary.id, intent.assigned_worker, "explore"
                ):
                    pinned_waiting.append(intent.id)
                    continue
                runnable_intents.append(intent)
            if job_blocked:
                self._log_changed(
                    f"{skip_scope}:job_blocked",
                    logging.DEBUG,
                    "skip project=%s has %s unclaimed intents blocked by running jobs",
                    summary.id,
                    job_blocked,
                )
            if pinned_waiting:
                self._log_changed(
                    f"{skip_scope}:pinned_waiting",
                    logging.INFO,
                    "skip project=%s pinned intents waiting for their assigned worker intents=%s",
                    summary.id,
                    sorted(pinned_waiting),
                )
            if runnable_intents:
                chosen = max(runnable_intents, key=self._intent_score)
                export_yaml = self.client.export_project(summary.id)
                return self._dispatch_explore(project, export_yaml, chosen)
        if project.project.reason is not None:
            self._log_changed(
                f"{skip_scope}:reason_claimed",
                logging.DEBUG,
                "skip reason project=%s because reason is already claimed by %s",
                summary.id,
                project.project.reason.worker,
            )
            return False
        self._log_changed(
            f"{skip_scope}:graph_unchanged",
            logging.DEBUG,
            "skip reason project=%s because reason state unchanged facts=%s hints=%s open_intents=%s intents=%s",
            summary.id,
            len(project.facts),
            len(project.hints),
            self._project_open_intent_count(project),
            len(project.intents),
        )
        return False

    def _dispatch_initial_project(self, project: ProjectDetail) -> bool:
        intent = self._get_bootstrap_intent(project)
        if intent is None:
            intent = self._create_bootstrap_intent(project.project.id)
            if intent is None:
                return False
        if self._project_has_running_bootstrap(project.project.id):
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_running",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap task is already running locally",
                project.project.id,
            )
            return False
        if intent.worker is not None:
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_claimed",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap intent=%s is already claimed by %s",
                project.project.id,
                intent.id,
                intent.worker,
            )
            return False
        return self._dispatch_bootstrap(project, intent)

    def _dispatch_reason(self, project: ProjectDetail, export_yaml: str, trigger: str) -> bool:
        gate = getattr(self, "reason_gate", None)
        signature = graph_signature(project, self.config.tasks.reason)
        if gate is not None and not gate.allows(project.project.id, signature):
            return False
        selection = self._select_worker(project.project.id, "reason")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:reason",
                logging.INFO,
                "no worker available for reason project=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:reason")
        claim = self.client.claim_reason(project.project.id, worker.name, trigger)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            if gate is not None:
                gate.started(project.project.id, signature)
            future = self.executor.submit(
                run_reason_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit reason task project=%s worker=%s", project.project.id, worker.name)
            self._best_effort_release_reason(project.project.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "reason",
            worker.name,
            cancellation,
            intent_id=None,
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            open_intent_count=self._project_open_intent_count(project),
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched reason project=%s worker=%s trigger=%s", project.project.id, worker.name, trigger)
        return True

    def _dispatch_bootstrap(self, project: ProjectDetail, intent: Intent) -> bool:
        selection = self._select_worker(project.project.id, "bootstrap")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:bootstrap",
                logging.INFO,
                "no worker available for bootstrap project=%s intent=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:bootstrap")
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_bootstrap_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                intent,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit bootstrap task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(project.project.id, "bootstrap", worker.name, cancellation, intent_id=intent.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched bootstrap project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        return True

    def _dispatch_explore(self, project: ProjectDetail, export_yaml: str, intent: Intent) -> bool:
        selection = self._select_worker(
            project.project.id,
            "explore",
            required_worker=intent.assigned_worker,
        )
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:explore",
                logging.INFO,
                "no worker available for explore project=%s intent=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:explore")
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_explore_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                intent,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit explore task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(project.project.id, "explore", worker.name, cancellation, intent_id=intent.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched explore project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        return True

    def _worker_available(self, project_id: str, worker_name: str, task_type: str) -> bool:
        """Whether the named worker could take a task right now (pin feasibility)."""
        now = time.time()
        running_counts = self._worker_counts()
        for worker in self.config.workers:
            if worker.name != worker_name:
                continue
            if task_type not in worker.task_types:
                return False
            if running_counts.get(worker.name, 0) >= worker.max_running:
                return False
            if self.worker_unhealthy_until.get(worker.name, 0) > now:
                return False
            if self.worker_rejected_until.get((project_id, task_type, worker.name), 0) > now:
                return False
            return True
        return False

    def _select_worker(
        self, project_id: str, task_type: str, required_worker: str | None = None
    ) -> WorkerSelection:
        now = time.time()
        candidates: list[WorkerConfig] = []
        blocked_busy: list[str] = []
        blocked_unhealthy: list[str] = []
        blocked_rejected: list[str] = []
        blocked_task_type: list[str] = []
        running_counts = self._worker_counts()
        for worker in self.config.workers:
            if required_worker is not None and worker.name != required_worker:
                continue
            if task_type not in worker.task_types:
                blocked_task_type.append(worker.name)
                continue
            running = running_counts.get(worker.name, 0)
            if running >= worker.max_running:
                blocked_busy.append(f"{worker.name}({running}/{worker.max_running})")
                continue
            unhealthy_until = self.worker_unhealthy_until.get(worker.name, 0)
            if unhealthy_until > now:
                blocked_unhealthy.append(f"{worker.name}({unhealthy_until - now:.1f}s)")
                continue
            rejected_until = self.worker_rejected_until.get((project_id, task_type, worker.name), 0)
            if rejected_until > now:
                blocked_rejected.append(f"{worker.name}({rejected_until - now:.1f}s)")
                continue
            candidates.append(worker)
        if not candidates:
            LOG.debug(
                "worker selection project=%s task=%s no candidates blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s",
                project_id,
                task_type,
                blocked_busy,
                blocked_unhealthy,
                blocked_rejected,
                blocked_task_type,
            )
            return WorkerSelection(
                worker=None,
                blocked_busy=blocked_busy,
                blocked_unhealthy=blocked_unhealthy,
                blocked_rejected=blocked_rejected,
                blocked_task_type=blocked_task_type,
            )
        ordered = choose_worker(candidates, running_counts)
        LOG.debug(
            "worker selection project=%s task=%s candidates=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s chosen=%s",
            project_id,
            task_type,
            [f"{worker.name}({running_counts.get(worker.name, 0)}/{worker.max_running},p{worker.priority})" for worker in candidates],
            blocked_busy,
            blocked_unhealthy,
            blocked_rejected,
            blocked_task_type,
            ordered[0].name if ordered else None,
        )
        return WorkerSelection(
            worker=ordered[0] if ordered else None,
            blocked_busy=blocked_busy,
            blocked_unhealthy=blocked_unhealthy,
            blocked_rejected=blocked_rejected,
            blocked_task_type=blocked_task_type,
        )

    def _worker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.futures.values():
            counts[task.worker_name] = counts.get(task.worker_name, 0) + 1
        return counts

    def _project_running_task_count(self, project_id: str) -> int:
        return sum(1 for task in self.futures.values() if task.project_id == project_id)

    def _project_running_task_summary(self, project_id: str) -> list[str]:
        summary: list[str] = []
        for task in self.futures.values():
            if task.project_id != project_id:
                continue
            if task.intent_id is None:
                summary.append(f"{task.task_type}:{task.worker_name}")
            else:
                summary.append(f"{task.task_type}:{task.worker_name}:{task.intent_id}")
        summary.sort()
        return summary

    def _project_has_running_bootstrap(self, project_id: str) -> bool:
        return any(task.project_id == project_id and task.task_type == "bootstrap" for task in self.futures.values())

    def _project_running_explore_intents(self, project_id: str) -> set[str]:
        return {
            task.intent_id
            for task in self.futures.values()
            if task.project_id == project_id and task.task_type == "explore" and task.intent_id is not None
        }

    def _running_project_count(self, summaries: list[ProjectSummary]) -> int:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        return len(self.runtime_project_ids & active_ids)

    def _project_open_intent_count(self, project: ProjectDetail) -> int:
        return sum(1 for intent in project.intents if intent.to is None and not intent.excluded)

    def _is_bootstrap_intent(self, intent: Intent) -> bool:
        return (
            intent.description == BOOTSTRAP_INTENT_DESCRIPTION
            and intent.creator == BOOTSTRAP_INTENT_CREATOR
            and intent.from_ == ["origin"]
            and intent.to is None
        )

    def _get_bootstrap_intent(self, project: ProjectDetail) -> Intent | None:
        intents = [intent for intent in project.intents if self._is_bootstrap_intent(intent)]
        if not intents:
            return None
        if len(intents) > 1:
            LOG.warning("project has multiple bootstrap intents project=%s intents=%s", project.project.id, [intent.id for intent in intents])
        intents.sort(key=lambda intent: (intent.worker is not None, intent.created_at, intent.id))
        return intents[0]

    def _is_initial_project(self, project: ProjectDetail) -> bool:
        fact_ids = {fact.id for fact in project.facts}
        if fact_ids != {"origin", "goal"} or len(project.facts) != 2:
            return False
        if not project.intents:
            return True
        return all(self._is_bootstrap_intent(intent) for intent in project.intents)

    def _project_requires_bootstrap(self, project: ProjectDetail) -> bool:
        if not project.project.bootstrap_enabled:
            return False
        bootstrap = self._get_bootstrap_intent(project)
        if bootstrap is not None:
            return not bootstrap.excluded
        return any("bootstrap" in worker.task_types for worker in self.config.workers)

    def _create_bootstrap_intent(self, project_id: str) -> Intent | None:
        response = self.client.create_intent(
            project_id,
            ["origin"],
            BOOTSTRAP_INTENT_DESCRIPTION,
            BOOTSTRAP_INTENT_CREATOR,
        )
        if response.status_code == 403:
            LOG.info("project became inactive before bootstrap intent create project=%s", project_id)
            return None
        if not response.ok:
            LOG.warning(
                "bootstrap intent write failed project=%s status=%s body=%s",
                project_id,
                response.status_code,
                response.text,
            )
            return None
        if not isinstance(response.data, dict):
            LOG.warning("bootstrap intent create returned empty body project=%s", project_id)
            return None
        intent = Intent.model_validate(response.data)
        LOG.info("created bootstrap intent project=%s intent=%s", project_id, intent.id)
        return intent

    def _reason_trigger(self, project: ProjectDetail) -> str | None:
        open_intent_count = self._project_open_intent_count(project)
        checkpoint = self.reason_checkpoints.get(project.project.id)
        if checkpoint is None:
            return "initial"
        changes: list[str] = []
        if len(project.facts) > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{len(project.facts)}")
        if len(project.hints) > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{len(project.hints)}")
        if checkpoint.open_intent_count > 0 and open_intent_count == 0:
            changes.append(f"open_intents:{checkpoint.open_intent_count}->0")
        if not changes:
            return None
        return ",".join(changes)

    @staticmethod
    def _intent_score(intent: Intent) -> float:
        """Value-first ordering with aging so nothing starves forever.

        ``score = priority * (1 + age_hours / 24)`` — a fresh high-priority
        intent always wins, but any neglected intent gains ~4% weight per hour
        until it surfaces.
        """
        priority = intent.priority or 50
        age_hours = 0.0
        try:
            created = datetime.strptime(intent.created_at, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 3600.0)
        except (ValueError, TypeError):
            pass
        return float(priority) * (1.0 + age_hours / 24.0)

    def _monitor_jobs(self, summaries: list[ProjectSummary]) -> None:
        """Poll running jobs, execute their check commands, enforce budgets.

        Check commands run detached on this host (local execution mode: the
        same host the workers launched the background work on). A check whose
        first stdout line starts with HIT or DONE finishes the job — the server
        then writes the outcome as a fact, which fires a reason pass through
        the normal facts-count trigger. Anything else is treated as a progress
        snippet and only updates ``last_output`` when it changed.
        """
        for future in [f for f in self._job_check_futures if f.done()]:
            key = self._job_check_futures.pop(future)
            self._job_checks_inflight.discard(key)

        if not hasattr(self, "_job_stop_futures"):
            self._job_stop_futures, self._job_stop_results = {}, {}
        for future in [f for f in self._job_stop_futures if f.done()]:
            key = self._job_stop_futures.pop(future)
            try:
                self._job_stop_results[key] = future.result()
            except Exception as exc:
                self._job_stop_results[key] = {"success": False, "error": str(exc)[:2000]}
        for key, result in list(self._job_stop_results.items()):
            response = self.client.record_job_stop(*key, result)
            if response.ok or response.status_code in (404, 409):
                del self._job_stop_results[key]

        for summary in summaries:
            try:
                jobs = self.client.list_jobs(summary.id)
            except requests.RequestException as exc:
                LOG.debug("job list failed project=%s error=%s", summary.id, exc)
                continue
            for job in jobs:
                key = (summary.id, job["id"])
                if job.get("stop_state") == "pending":
                    if key not in self._job_stop_futures.values() and key not in self._job_stop_results:
                        future = self.cleanup_executor.submit(
                            self._run_job_stop, summary.id, job["id"], job.get("stop_command") or ""
                        )
                        self._job_stop_futures[future] = key
                # Suspension permits cleanup only, never a new check/probe.
                if summary.status != "active" or job.get("status") != "running":
                    continue
                budget = job.get("budget_seconds")
                created_at = job.get("created_at")
                if budget and created_at and self._seconds_since(created_at) >= budget:
                    last_output = (job.get("last_output") or "none")[:200]
                    last_check = job.get("last_check_at") or "never"
                    self._finish_job(
                        summary.id,
                        job,
                        "exhausted",
                        f"budget of {budget}s expired before producing a result (job ran {int(self._seconds_since(created_at))}s, last check {last_check}); last output: {last_output}",
                    )
                    continue
                check_command = (job.get("check_command") or "").strip()
                if not check_command:
                    continue
                key = (summary.id, job["id"])
                if key in self._job_checks_inflight:
                    continue
                self._job_checks_inflight.add(key)
                future = self.cleanup_executor.submit(
                    self._run_job_check, summary.id, job, check_command
                )
                self._job_check_futures[future] = key

    def _run_job_check(self, project_id: str, job: dict, check_command: str) -> None:
        try:
            proc = subprocess.run(
                check_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=JOBS_CHECK_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return
        except Exception:
            LOG.exception("job check crashed project=%s job=%s", project_id, job.get("id"))
            return
        output = (proc.stdout or "").strip()
        if not output:
            return
        first_line = output.splitlines()[0].strip()
        upper = first_line.upper()
        if upper.startswith("HIT"):
            self._finish_job(project_id, job, "hit", output[:JOBS_RESULT_MAX_CHARS])
        elif upper.startswith("DONE"):
            self._finish_job(project_id, job, "done", output[:JOBS_RESULT_MAX_CHARS])
        else:
            snippet = output[:JOBS_PROGRESS_MAX_CHARS]
            if snippet != (job.get("last_output") or ""):
                self.client.progress_job(project_id, job["id"], snippet)

    def _finish_job(self, project_id: str, job: dict, status: str, result: str) -> None:
        response = self.client.finish_job(project_id, job["id"], status, result)
        if not response.ok and response.status_code != 409:
            LOG.warning(
                "job finish failed project=%s job=%s status=%s http=%s body=%s",
                project_id,
                job["id"],
                status,
                response.status_code,
                response.text[:300],
            )
            return
        if response.ok:
            LOG.info("job finished project=%s job=%s outcome=%s", project_id, job["id"], status)
            future = self.cleanup_executor.submit(
                persist_project_reports, self.client, self.container_manager, project_id
            )
            self.cleanup_futures[future] = (f"job-reports-{project_id}-{job['id']}", None, None)

    def _run_job_stop(self, project_id: str, job_id: str, stop_command: str) -> dict:
        if not stop_command.strip():
            return {"success": False, "error": "No stop command registered; stop the process manually"}
        try:
            proc = subprocess.run(
                stop_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=JOBS_CHECK_TIMEOUT_SECONDS,
            )
            LOG.info(
                "job stop executed project=%s job=%s rc=%s stdout=%s stderr=%s",
                project_id,
                job_id,
                proc.returncode,
                (proc.stdout or "").strip()[:200],
                (proc.stderr or "").strip()[:200],
            )
            return {"success": proc.returncode == 0,
                    "error": "" if proc.returncode == 0 else f"Exit {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:1800]}"}
        except Exception as exc:
            LOG.exception("job stop crashed project=%s job=%s", project_id, job_id)
            return {"success": False, "error": str(exc)[:2000]}

    def _acknowledge_suspensions(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "stopped" or not summary.suspension_id or summary.suspension_ack_id == summary.suspension_id:
                continue
            if any(task.project_id == summary.id for task in self.futures.values()):
                continue
            if any(key[0] == summary.id for key in self._job_checks_inflight):
                continue
            if self._inactive_cleanup_done.get(summary.id) != "stopped":
                continue
            self.client.acknowledge_suspension(summary.id, summary.suspension_id)

    @staticmethod
    def _seconds_since(timestamp: str) -> float:
        try:
            then = datetime.strptime(timestamp, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return 0.0
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())

    def _reap_futures(self) -> None:
        done = [future for future in self.futures if future.done()]
        for future in done:
            task = self.futures.pop(future)
            try:
                outcome = future.result()
                self._record_task_outcome(task, outcome)
                if outcome == "cancelled":
                    LOG.info(
                        "task cancelled project=%s task=%s worker=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                    )
                elif outcome != "success":
                    LOG.warning(
                        "task finished project=%s task=%s worker=%s outcome=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        outcome,
                    )
                self._clear_project_log_state(task.project_id)
                if outcome == "unhealthy":
                    retry_after_seconds = UNHEALTHY_RETRY_AFTER_SECONDS
                    self.worker_unhealthy_until[task.worker_name] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked unhealthy worker=%s retry_after=%.0fs",
                        task.worker_name,
                        retry_after_seconds,
                    )
                else:
                    self.worker_unhealthy_until.pop(task.worker_name, None)
                rejection_key = (task.project_id, task.task_type, task.worker_name)
                if outcome == "rejected":
                    retry_after_seconds = REJECTED_RETRY_AFTER_SECONDS
                    self.worker_rejected_until[rejection_key] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked rejected project=%s task=%s worker=%s retry_after=%.0fs",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        retry_after_seconds,
                    )
                else:
                    self.worker_rejected_until.pop(rejection_key, None)
                if outcome == "success" and task.task_type == "reason":
                    assert task.fact_count is not None
                    assert task.hint_count is not None
                    assert task.open_intent_count is not None
                    self.reason_checkpoints[task.project_id] = ReasonCheckpoint(
                        fact_count=task.fact_count,
                        hint_count=task.hint_count,
                        open_intent_count=task.open_intent_count,
                    )
                    LOG.debug(
                        "reason checkpoint updated project=%s facts=%s hints=%s open_intents=%s",
                        task.project_id,
                        task.fact_count,
                        task.hint_count,
                        task.open_intent_count,
                    )
            except Exception:
                LOG.exception("task crashed project=%s task=%s worker=%s", task.project_id, task.task_type, task.worker_name)
                self._record_task_outcome(task, "crashed")

    def _record_task_outcome(self, task: RunningTask, outcome: str) -> None:
        gate = getattr(self, "reason_gate", None)
        if task.task_type == "reason" and gate is not None:
            try:
                gate.finished(task.project_id, outcome, {
                    "fact_count": task.fact_count, "hint_count": task.hint_count,
                    "open_intent_count": task.open_intent_count,
                })
            except Exception:
                # Keep a fail-closed in-memory gate if durable state cannot be saved.
                gate.states[task.project_id] = {"unreadable": True}
                LOG.exception("could not persist planner outcome project=%s", task.project_id)
        if gate is None:
            return
        event = {"event": "task", "task_type": task.task_type, "intent_id": task.intent_id,
                 "worker": task.worker_name, "outcome": outcome,
                 "elapsed_seconds": round(time.time() - task.started_at, 3)}
        try:
            current = self.client.get_project(task.project_id)
            event.update(project_status=current.project.status, facts=len(current.facts),
                         closed_intents=sum(i.to is not None for i in current.intents),
                         open_intents=self._project_open_intent_count(current))
        except Exception:
            LOG.warning("could not read graph for task metrics project=%s", task.project_id)
        record_metric(self.container_manager, task.project_id, event)

    def _cleanup_completed_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "completed":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_completed_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_completed, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _cleanup_stopped_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status not in ("stopped", "aborted"):
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_stopped_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_stopped, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _persist_aborted_reports(self, summaries: list[ProjectSummary]) -> None:
        """Aborted projects get the full report set written, like completed ones.

        Abandoning a run must not lose its record: the dispatcher notices the
        terminal ``aborted`` status and persists timeline, graph and both report
        flavours into the project's durable ``reports/`` directory.
        """
        for summary in summaries:
            if summary.status != "aborted" or summary.id in self._abort_reports_done:
                continue
            self._abort_reports_done.add(summary.id)
            LOG.info("persisting reports for aborted project project=%s", summary.id)
            future = self.cleanup_executor.submit(
                persist_project_reports, self.client, self.container_manager, summary.id
            )
            self.cleanup_futures[future] = (f"abort-reports-{summary.id}", None, None)

    def _queue_container_cleanups(self, summaries: list[ProjectSummary]) -> None:
        self._cleanup_completed_containers(summaries)
        self._cleanup_stopped_containers(summaries)

    def _reap_cleanup_futures(self) -> None:
        done = [future for future in self.cleanup_futures if future.done()]
        for future in done:
            name, project_id, target_status = self.cleanup_futures.pop(future)
            self._cleanup_pending.discard(name)
            try:
                success = future.result()
                if success and project_id is not None and target_status in ("completed", "stopped"):
                    self._inactive_cleanup_done[project_id] = target_status
                elif project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
            except Exception:
                if project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
                LOG.exception("container cleanup failed container=%s", name)

    def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        self.runtime_project_ids.intersection_update(active_ids)
        inactive_status_by_id = {summary.id: summary.status for summary in summaries if summary.status != "active"}
        for project_id, status in list(self._inactive_cleanup_done.items()):
            current_status = inactive_status_by_id.get(project_id)
            if current_status != status:
                self._inactive_cleanup_done.pop(project_id, None)

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        status_by_project = {summary.id: summary.status for summary in summaries}
        for task in self.futures.values():
            status = status_by_project.get(task.project_id, "deleted")
            if status != "active" and task.cancellation.cancel(status):
                LOG.info(
                    "cancelling running task for inactive project project=%s task=%s worker=%s status=%s",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                    status,
                )

    def _initialize_reason_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "active":
                continue
            if summary.id in self.reason_checkpoints:
                continue
            open_intent_count = summary.working_intent_count + summary.unclaimed_intent_count
            gate = getattr(self, "reason_gate", None)
            if gate is not None:
                state = gate.state(summary.id)
                saved = state.get("checkpoint")
                if state.get("succeeded") and saved:
                    self.reason_checkpoints[summary.id] = ReasonCheckpoint(**saved)
                    continue
                # Fresh deployments should fill existing vacancies. Failed plans
                # must retain their opportunity to retry, subject to the gate.
                if state or open_intent_count < self.config.tasks.reason.target_open_intents:
                    continue
            if open_intent_count == 0:
                continue
            self.reason_checkpoints[summary.id] = ReasonCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                open_intent_count=open_intent_count,
            )
            LOG.debug(
                "reason checkpoint initialized project=%s facts=%s hints=%s open_intents=%s",
                summary.id,
                summary.fact_count,
                summary.hint_count,
                open_intent_count,
            )

    def _best_effort_release(self, project_id: str, intent_id: str, worker_name: str) -> None:
        response = self.client.release(project_id, intent_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("release failed project=%s intent=%s worker=%s status=%s", project_id, intent_id, worker_name, response.status_code)

    def _best_effort_release_reason(self, project_id: str, worker_name: str) -> None:
        response = self.client.release_reason(project_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("reason release failed project=%s worker=%s status=%s", project_id, worker_name, response.status_code)

    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        state = (level, message, args)
        if self._log_state.get(scope) == state:
            return
        self._log_state[scope] = state
        LOG.log(level, message, *args)

    def _clear_log_state(self, scope: str) -> None:
        self._log_state.pop(scope, None)

    def _clear_project_log_state(self, project_id: str) -> None:
        prefix = f"project:{project_id}:"
        for scope in list(self._log_state):
            if scope.startswith(prefix):
                self._log_state.pop(scope, None)

    def _validate_server_settings(self) -> None:
        settings = self.client.get_settings()
        interval = self.config.runtime.interval
        for name, value in (("intent_timeout", settings.intent_timeout), ("reason_timeout", settings.reason_timeout)):
            if value <= interval:
                raise RuntimeError(
                    f"server {name}={value}s must be greater than dispatcher interval={interval}s"
                )
            if value < interval * 2:
                LOG.warning(
                    "server %s is tight %s=%ss interval=%ss; heartbeat slack is only %ss",
                    name,
                    name,
                    value,
                    interval,
                    value - interval,
                )
                continue
            LOG.info(
                "server setting validated %s=%ss interval=%ss",
                name,
                value,
                interval,
            )

    def _run_startup_healthchecks(self, *, show_commands: bool) -> None:
        results = run_startup_healthchecks(self.config, show_commands=show_commands)
        if any(result.ok for result in results):
            return
        raise RuntimeError(format_failure_summary(results))
