from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Any

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import pytest

from board.dispatcher.config import DispatchConfig
from board.dispatcher.models import ReasonCheckpoint
from board.dispatcher.protocol.client import ApiResult
from board.dispatcher.runtime.process import ProcessResult
from board.dispatcher.scheduler.loop import DispatcherLoop
from board.reporting import paths as reporting_paths
from board.server import db
from board.server.app import app
from board.server.models import ProjectDetail, ProjectSummary, Settings


class InProcessClient:
    def __init__(self, http: TestClient):
        self.http = http
        self._summaries = TypeAdapter(list[ProjectSummary])

    def close(self) -> None:
        return None

    def list_projects(self) -> list[ProjectSummary]:
        response = self.http.get("/projects")
        response.raise_for_status()
        return self._summaries.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self.http.get(f"/projects/{project_id}")
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self.http.get("/settings")
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str, format: str = "yaml") -> str:
        response = self.http.get(f"/projects/{project_id}/export?format={format}")
        response.raise_for_status()
        return response.text

    def get_report(self, project_id: str, report_type: str) -> ApiResult:
        response = self.http.get(f"/projects/{project_id}/report?type={report_type}")
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        return ApiResult(response.status_code, data, response.text)

    def get_asset_map(self, project_id: str, format: str) -> ApiResult:
        response = self.http.get(f"/projects/{project_id}/asset-map?format={format}")
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        return ApiResult(response.status_code, data, response.text)

    def register_evidence(self, project_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._post(f"/projects/{project_id}/evidence", payload)

    def create_finding(self, project_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._post(f"/projects/{project_id}/findings", payload)

    def heartbeat(self, project_id: str, intent_id: str, worker: str, *, request_timeout: float | None = None) -> ApiResult:
        return self._post(f"/projects/{project_id}/intents/{intent_id}/heartbeat", {"worker": worker})

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/reason/claim", {"worker": worker, "trigger": trigger})

    def reason_heartbeat(self, project_id: str, worker: str, lease_id: str | None = None, *, request_timeout: float | None = None) -> ApiResult:
        return self._post(f"/projects/{project_id}/reason/heartbeat", {"worker": worker, "lease_id": lease_id})

    def release_reason(self, project_id: str, worker: str, lease_id: str | None = None) -> ApiResult:
        return self._post(f"/projects/{project_id}/reason/release", {"worker": worker, "lease_id": lease_id})

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/intents/{intent_id}/release", {"worker": worker})

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            {"worker": worker, "description": description},
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/complete",
            {"from": from_ids, "description": description, "worker": worker},
        )

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str, priority: int | None = None, blocked_by: str | None = None) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/intents",
            {"from": from_ids, "description": description, "creator": creator, "worker": None},
        )

    def _post(self, path: str, payload: dict[str, Any]) -> ApiResult:
        response = self.http.post(path, json=payload)
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        return ApiResult(response.status_code, data, response.text)


class LocalProcess:
    def __init__(self, command: list[str], env: dict[str, str]):
        self.command = command
        self.env = env
        self._process: subprocess.Popen[str] | None = None
        self._cancel_reason: str | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._process = subprocess.Popen(
                self.command,
                env={**os.environ, **self.env},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        timed_out = False
        try:
            stdout, stderr = self._process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill()
            stdout, stderr = self._process.communicate()
        return ProcessResult(
            returncode=self._process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()


class LocalContainerManager:
    """Tmp-dir backed backend so the e2e run exercises the real evidence/reports layout."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []
        self._root = Path(tempfile.mkdtemp(prefix="board-e2e-"))

    def close(self) -> None:
        return None

    @property
    def root(self) -> Path:
        return self._root

    def container_name(self, project_id: str) -> str:
        return f"local-{project_id}"

    def ensure_running(self, project_id: str) -> str:
        return self.container_name(project_id)

    def build_exec_process(
        self,
        _container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> LocalProcess:
        assert timeout_seconds is not None
        assert kill_after_seconds == 5
        return LocalProcess(command, env)

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.writes.append((container_name, path, content))

    def scratch_dir(self, project_id: str) -> str:
        directory = reporting_paths.scratch_dir(self._root, project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    def worker_env(self, project_id: str, intent_id: str | None = None) -> dict[str, str]:
        return {
            "BOARD_PROJECT_DIR": str(reporting_paths.project_dir(self._root, project_id)),
            "BOARD_EVIDENCE_DIR": str(self.evidence_dir(project_id, intent_id)),
        }

    def evidence_dir(self, project_id: str, intent_id: str | None = None) -> Path:
        directory = (
            reporting_paths.intent_evidence_dir(self._root, project_id, intent_id)
            if intent_id
            else reporting_paths.evidence_dir(self._root, project_id)
        )
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def reports_dir(self, project_id: str) -> Path:
        directory = reporting_paths.reports_dir(self._root, project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def evidence_relative_path(self, project_id: str, intent_id: str | None, file_path: Path) -> str:
        return reporting_paths.evidence_relative_path(
            self._root, project_id, intent_id, file_path.relative_to(
                reporting_paths.intent_evidence_dir(self._root, project_id, intent_id)
            ).as_posix()
        )

    def needs_completed_cleanup(self, _project_id: str) -> bool:
        return False

    def needs_stopped_cleanup(self, _project_id: str) -> bool:
        return False

    def managed_container_names(self) -> list[str]:
        return []


@pytest.fixture
def http_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "board.db")
    with TestClient(app) as client:
        yield client


def _phase(
    outcome: str,
    *,
    rules: list[dict[str, Any]] | None = None,
    zero_outcomes: list[str] | None = None,
) -> str:
    outcomes = {name: 0 for name in zero_outcomes or []}
    outcomes[outcome] = 1
    payload: dict[str, Any] = {"delay": [0, 0], "outcomes": outcomes}
    if rules is not None:
        payload["rules"] = rules
    return json.dumps(payload)


def _config(
    *,
    bootstrap: str,
    reason: str,
    explore: str,
    task_types: list[str] | None = None,
    worker_healthcheck: str = "startup_only",
    healthcheck: str | None = None,
) -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "interval": 1,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 2,
                "worker_healthcheck": worker_healthcheck,
                "prompt_group": "mock",
            },
            "tasks": {
                "bootstrap": {"timeout": 2, "conclude_timeout": 2},
                "reason": {"timeout": 2, "max_intents": 1},
                "explore": {"timeout": 2, "conclude_timeout": 2},
            },
            "container": {
                "image": "unused",
                "network_mode": "host",
                "completed_action": "stop",
                "evidence_root": "/tmp/board-test-projects",
            },
            "workers": [
                {
                    "name": "mock-worker",
                    "type": "mock",
                    "task_types": task_types or ["bootstrap", "reason", "explore"],
                    "max_running": 1,
                    "priority": 0,
                    "env": {
                        "MOCK_HEALTHCHECK": healthcheck or _phase("ok"),
                        "MOCK_BOOTSTRAP": bootstrap,
                        "MOCK_REASON": reason,
                        "MOCK_EXPLORE_EXECUTE": explore,
                    },
                }
            ],
        }
    )


def _loop(config: DispatchConfig, client: InProcessClient, containers: LocalContainerManager) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = config
    loop.client = client
    loop.container_manager = containers
    loop.executor = ThreadPoolExecutor(max_workers=config.runtime.max_workers)
    loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
    loop.futures = {}
    loop.cleanup_futures = {}
    loop.reason_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._log_state = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop._terminal_reports_done = set()
    loop._terminal_report_keys = {}
    loop.project_cursor = 0
    return loop


def _dispatch_and_wait(loop: DispatcherLoop) -> None:
    loop._reap_futures()
    summaries = loop.client.list_projects()
    loop._initialize_reason_checkpoints(summaries)
    loop._refresh_runtime_projects(summaries)
    loop._cancel_inactive_tasks(summaries)
    loop._queue_container_cleanups(summaries)
    loop._dispatch_available(summaries)
    assert loop.futures
    for future in list(loop.futures):
        future.result(timeout=5)
    loop._reap_futures()


def _create_project(http: TestClient) -> str:
    response = http.post(
        "/projects",
        json={"title": "integration", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def test_mock_scheduler_bootstrap_completes_project_end_to_end(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [fact.id for fact in project.facts] == ["origin", "goal", "f001"]
    assert [(intent.id, intent.to) for intent in project.intents] == [("i001", "f001"), ("i002", "goal")]


def test_mock_scheduler_runs_reason_explore_reason_complete_chain(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("intent", rules=[{"fact_ids_gte": 3, "force": "complete"}]),
            explore=_phase("fact"),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)
    seed = client.create_intent(project_id, ["origin"], "seed", "seed-worker")
    assert seed.ok
    assert client.heartbeat(project_id, "i001", "seed-worker").ok
    assert client.conclude(project_id, "i001", "seed-worker", "seed fact").ok

    try:
        _dispatch_and_wait(loop)
        assert loop.reason_checkpoints[project_id] == ReasonCheckpoint(3, 0, 0)
        _dispatch_and_wait(loop)
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [fact.id for fact in project.facts] == ["origin", "goal", "f001", "f002"]
    assert [(intent.id, intent.to) for intent in project.intents] == [
        ("i001", "f001"),
        ("i002", "f002"),
        ("i003", "goal"),
    ]
    assert any("graph-reason_execute-" in path and "f002" in content for _, path, content in containers.writes)
    assert any("graph-explore_execute-" in path and "f001" in content for _, path, content in containers.writes)

    # evidence pipeline: the explore worker wrote to $BOARD_EVIDENCE_DIR and the
    # dispatcher registered the artefact with a real sha256 against the produced fact
    evidence = http_client.get(f"/projects/{project_id}/evidence").json()
    assert len(evidence) == 1
    assert evidence[0]["path"] == "proj_001/evidence/i002/mock_capture.txt"
    assert evidence[0]["fact_id"] == "f002"
    assert evidence[0]["intent_id"] == "i002"
    assert len(evidence[0]["sha256"]) == 64

    # worker-declared findings ride along with the fact
    findings = http_client.get(f"/projects/{project_id}/findings").json()
    assert [item["fact_id"] for item in findings] == ["f002"]
    assert findings[0]["intent_id"] == "i002"

    # reports auto-persisted on completion, no human action (AC2)
    reports_dir = containers.root / "proj_001" / "reports"
    assert sorted(p.name for p in reports_dir.iterdir()) == [
        "asset-inventory.md",
        "asset-map.html",
        "asset-map.json",
        "graph.yaml",
        "report-agent.json",
        "report-agent.md",
        "report-human.md",
        "timeline.txt",
    ]
    agent_json = json.loads((reports_dir / "report-agent.json").read_text())
    assert agent_json["header"]["project_id"] == "proj_001"
    assert agent_json["evidence"][0]["path"] == "proj_001/evidence/i002/mock_capture.txt"


def test_mock_scheduler_enabled_project_skips_bootstrap_when_worker_does_not_support_it(
    http_client: TestClient,
) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
            task_types=["reason", "explore"],
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [(intent.description, intent.to) for intent in project.intents] == [
        ("mock complete from origin", "goal")
    ]


def test_task_healthcheck_healthy_worker_completes_end_to_end(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
            worker_healthcheck="startup_and_task",
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    # code-based check_health runs before the task, passes, and the bootstrap completes
    assert project.project.status == "completed"


def test_task_healthcheck_failure_aborts_task_and_cools_down_worker(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
            worker_healthcheck="startup_and_task",
            healthcheck=_phase("fail", zero_outcomes=["ok"]),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    # unhealthy worker -> task aborted before execution, no facts written, worker put on cooldown
    assert project.project.status == "active"
    assert [fact.id for fact in project.facts] == ["origin", "goal"]
    assert "mock-worker" in loop.worker_unhealthy_until


def _failover_config() -> DispatchConfig:
    def worker(name: str, priority: int, healthcheck: str) -> dict:
        return {
            "name": name,
            "type": "mock",
            "task_types": ["bootstrap", "reason", "explore"],
            "max_running": 1,
            "priority": priority,
            "env": {
                "MOCK_HEALTHCHECK": healthcheck,
                "MOCK_BOOTSTRAP": _phase("complete"),
                "MOCK_REASON": _phase("complete", zero_outcomes=["intent"]),
                "MOCK_EXPLORE_EXECUTE": _phase("fact"),
            },
        }

    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "interval": 1,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 2,
                "worker_healthcheck": "startup_and_task",
                "prompt_group": "mock",
            },
            "tasks": {
                "bootstrap": {"timeout": 2, "conclude_timeout": 2},
                "reason": {"timeout": 2, "max_intents": 1},
                "explore": {"timeout": 2, "conclude_timeout": 2},
            },
            "container": {"image": "unused", "network_mode": "host", "completed_action": "stop", "evidence_root": "/tmp/board-test-projects"},
            "workers": [
                worker("bad", 0, _phase("fail", zero_outcomes=["ok"])),
                worker("good", 1, _phase("ok")),
            ],
        }
    )


def test_unhealthy_worker_fails_over_to_healthy_worker(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(_failover_config(), client, containers)
    project_id = _create_project(http_client)

    try:
        # round 1: 'bad' (priority 0) is chosen first, its health check fails -> cooldown
        _dispatch_and_wait(loop)
        assert "bad" in loop.worker_unhealthy_until
        assert client.get_project(project_id).project.status == "active"

        # round 2: 'bad' still cooling down -> 'good' takes over and completes the project
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert any(intent.worker == "good" for intent in project.intents)


@pytest.mark.parametrize("outcome", ["aborted", "current_results"])
def test_terminal_project_gets_full_report_set_persisted(http_client: TestClient, outcome) -> None:
    # The dispatcher notices the terminal 'aborted' status on its next poll and
    # writes the full report set — abandoning a run must not lose its record.
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)
    if outcome == "aborted":
        response = http_client.post(
            f"/projects/{project_id}/abort",
            json={"reason": "foreign-tenant account unobtainable", "actor": "F"},
        )
    else:
        response = http_client.post(f"/projects/{project_id}/complete", json={
            "from": ["origin"], "description": "Coverage gaps remain", "worker": "F", "outcome": outcome})
    assert response.status_code == 200

    try:
        loop._persist_terminal_reports(client.list_projects())
        for future in loop.cleanup_futures:
            future.result(timeout=10)
        loop._reap_cleanup_futures()
    finally:
        loop.close()

    reports_dir = containers.root / project_id / "reports"
    assert sorted(p.name for p in reports_dir.iterdir()) == [
        "asset-inventory.md",
        "asset-map.html",
        "asset-map.json",
        "graph.yaml",
        "report-agent.json",
        "report-agent.md",
        "report-human.md",
        "timeline.txt",
    ]
    agent = json.loads((reports_dir / "report-agent.json").read_text())
    if outcome == "aborted":
        assert agent["header"]["status"] == "aborted"
        assert agent["header"]["abort_reason"] == "foreign-tenant account unobtainable"
        assert "PROJECT ABORTED by F" in (reports_dir / "timeline.txt").read_text()
        assert "**Outcome.**" in (reports_dir / "report-human.md").read_text()
    else:
        assert agent["header"]["completion_outcome"] == "current_results"
        assert "original goal is not asserted to have been achieved" in (reports_dir / "report-human.md").read_text()
        assert "PROJECT CONCLUDED WITH CURRENT RESULTS" in (reports_dir / "timeline.txt").read_text()
        assert "completion_outcome: current_results" in (reports_dir / "graph.yaml").read_text()
        assert json.loads((reports_dir / "asset-map.json").read_text())["project"]["completion_outcome"] == "current_results"
        assert "original goal is not asserted to have been achieved" in (reports_dir / "asset-inventory.md").read_text()
        assert "original goal not asserted achieved" in (reports_dir / "asset-map.html").read_text()

    # persistence is submitted once per project, not every poll
    loop2 = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
        ),
        client,
        containers,
    )
    try:
        loop2._terminal_reports_done = set(loop._terminal_reports_done)
        before = (reports_dir / "timeline.txt").stat().st_mtime_ns
        loop2._persist_terminal_reports(client.list_projects())
        loop2._reap_cleanup_futures()
    finally:
        loop2.close()
    assert (reports_dir / "timeline.txt").stat().st_mtime_ns == before


def test_terminal_reports_refresh_after_reopen_and_reconclude(http_client):
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(_config(bootstrap=_phase("complete"), reason=_phase("complete", zero_outcomes=["intent"]), explore=_phase("fact")), client, containers)
    pid = _create_project(http_client)
    try:
        for generation, outcome in enumerate(("current_results", "goal_met")):
            assert http_client.post(f"/projects/{pid}/complete", json={"from":["origin"], "description":f"Conclusion {generation}", "worker":"human", "outcome":outcome}).status_code == 200
            loop._persist_terminal_reports(client.list_projects())
            for future in loop.cleanup_futures:
                future.result(timeout=10)
            loop._reap_cleanup_futures()
            report = json.loads((containers.root / pid / "reports/report-agent.json").read_text())
            assert report["header"]["completion_outcome"] == outcome
            if generation == 0:
                assert http_client.post(f"/projects/{pid}/reopen", json={"description":"New evidence", "creator":"human"}).status_code == 200
    finally:
        loop.close()


def test_failed_terminal_report_write_retries_before_completed_cleanup(http_client, monkeypatch):
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(_config(bootstrap=_phase("complete"), reason=_phase("complete", zero_outcomes=["intent"]), explore=_phase("fact")), client, containers)
    pid = _create_project(http_client)
    assert http_client.post(f"/projects/{pid}/complete", json={"from":["origin"], "description":"Concluded", "worker":"human", "outcome":"current_results"}).status_code == 200
    attempts = []
    cleanups = []
    monkeypatch.setattr("board.dispatcher.scheduler.loop.persist_project_reports", lambda *_: attempts.append(True) or len(attempts) > 1)
    monkeypatch.setattr(containers, "needs_completed_cleanup", lambda *_: True)
    monkeypatch.setattr(containers, "cleanup_completed", lambda *_: cleanups.append(True) or True, raising=False)
    summaries = client.list_projects()
    try:
        for attempt in (1, 2):
            loop._cleanup_completed_containers(summaries)
            assert cleanups == []
            loop._persist_terminal_reports(summaries)
            for future in loop.cleanup_futures:
                future.result(timeout=10)
            loop._reap_cleanup_futures()
            assert len(attempts) == attempt
        loop._cleanup_completed_containers(summaries)
        for future in loop.cleanup_futures:
            future.result(timeout=10)
        loop._reap_cleanup_futures()
        assert cleanups == [True]
    finally:
        loop.close()
