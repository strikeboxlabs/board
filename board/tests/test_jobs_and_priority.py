from __future__ import annotations

from concurrent.futures import Future

from fastapi.testclient import TestClient
import pytest

from board.dispatcher.contracts import parse_json_output, validate_explore_payload, validate_optional_jobs, validate_reason_payload
from board.dispatcher.models import ReasonCheckpoint
from board.dispatcher.scheduler.loop import DispatcherLoop
from board.server import db
from board.server.app import app
from board.server.models import Fact, ProjectSummary

from conftest import make_config, make_intent, make_project


def _loop() -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.reason_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.cleanup_futures = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._log_state = {}
    loop.project_cursor = 0
    loop._job_checks_inflight = set()
    loop._job_check_futures = {}
    return loop


def _summary(project_id: str, status: str = "active") -> ProjectSummary:
    return ProjectSummary(
        id=project_id,
        title=project_id,
        status=status,
        bootstrap_enabled=True,
        created_at="2026-01-01T00:00:00Z",
        fact_count=2,
        intent_count=0,
        working_intent_count=0,
        unclaimed_intent_count=0,
        hint_count=0,
    )


# ---------------------------------------------------------------- scoring


def test_intent_score_prefers_priority_at_equal_age() -> None:
    loop = _loop()
    high = make_intent("i001")
    high.priority = 90
    low = make_intent("i002")
    low.priority = 10
    assert loop._intent_score(high) > loop._intent_score(low)


def test_intent_score_aging_rescues_neglected_intents() -> None:
    from datetime import datetime, timedelta, timezone

    loop = _loop()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_mid = make_intent("i001")
    fresh_mid.priority = 50
    fresh_mid.created_at = now
    old_low = make_intent("i002")
    old_low.priority = 10
    old_low.created_at = five_days_ago  # ~120h old: 10 * (1 + 120/24) = 60 > 50
    assert loop._intent_score(old_low) > loop._intent_score(fresh_mid)


def test_intent_score_tolerates_malformed_timestamps() -> None:
    loop = _loop()
    intent = make_intent()
    intent.created_at = "not-a-timestamp"
    assert loop._intent_score(intent) == 50.0


# ---------------------------------------------------------------- dispatch gating


def test_job_blocked_intent_is_not_dispatched() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    project = make_project(intents=[])
    blocked = make_intent("i001")
    blocked.worker = None
    blocked.priority = 90
    blocked.blocked_by = "j001"
    free = make_intent("i002")
    free.worker = None
    free.priority = 10
    project.intents = [blocked, free]
    from board.server.models import Job

    project.jobs = [
        Job(
            id="j001",
            project_id="proj_001",
            description="running spray",
            status="running",
            creator="test",
            created_at="2026-09-06T00:00:00Z",
        )
    ]
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(fact_count=3, hint_count=1, open_intent_count=2)
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    chosen: list[str] = []
    loop._dispatch_explore = lambda _project, _graph, intent: chosen.append(intent.id) or True

    assert loop._try_dispatch_project(_summary("proj_001"))
    assert chosen == ["i002"]  # blocked i001 skipped despite priority 90


def test_blocked_intent_dispatches_after_job_finishes() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    project = make_project(intents=[])
    blocked = make_intent("i001")
    blocked.worker = None
    blocked.blocked_by = "j001"
    project.intents = [blocked]
    from board.server.models import Job

    project.jobs = [
        Job(
            id="j001",
            project_id="proj_001",
            description="finished spray",
            status="hit",
            creator="test",
            created_at="2026-09-06T00:00:00Z",
        )
    ]
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(fact_count=3, hint_count=1, open_intent_count=1)
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    chosen: list[str] = []
    loop._dispatch_explore = lambda _project, _graph, intent: chosen.append(intent.id) or True

    assert loop._try_dispatch_project(_summary("proj_001"))
    assert chosen == ["i001"]


# ---------------------------------------------------------------- job monitor


class _FakeClient:
    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = jobs
        self.finished: list[tuple[str, str, str]] = []
        self.progressed: list[tuple[str, str, str]] = []

    def list_jobs(self, project_id: str) -> list[dict]:
        return self.jobs

    def finish_job(self, project_id: str, job_id: str, status: str, result: str):
        self.finished.append((job_id, status, result))
        return type("R", (), {"ok": True, "status_code": 200, "text": ""})()

    def progress_job(self, project_id: str, job_id: str, output: str):
        self.progressed.append((job_id, output))
        return type("R", (), {"ok": True, "status_code": 200, "text": ""})()


def _monitor_loop(client: _FakeClient) -> DispatcherLoop:
    loop = _loop()
    loop._job_stops_done = set()
    loop.client = client
    loop.cleanup_executor = type(
        "Exec",
        (),
        {
            "submit": lambda _self, fn, *a, **k: (
                fn(*a, **k),
                Future(),
            )[1],
        },
    )()
    loop.container_manager = type("Containers", (), {})()
    return loop


def test_job_check_hit_line_finishes_job() -> None:
    client = _FakeClient([])
    loop = _monitor_loop(client)
    job = {"id": "j001", "status": "running", "check_command": "echo 'HIT admin:hunter2'", "last_output": None}
    loop._run_job_check("proj_001", job, job["check_command"])
    assert client.finished == [("j001", "hit", "HIT admin:hunter2")]


def test_job_check_done_line_finishes_job() -> None:
    client = _FakeClient([])
    loop = _monitor_loop(client)
    job = {"id": "j001", "status": "running", "check_command": "echo DONE exhausted", "last_output": None}
    loop._run_job_check("proj_001", job, job["check_command"])
    assert client.finished[0][:2] == ("j001", "done")


def test_job_check_progress_line_updates_output() -> None:
    client = _FakeClient([])
    loop = _monitor_loop(client)
    job = {"id": "j001", "status": "running", "check_command": "echo 'RUNNING 10/100'", "last_output": None}
    loop._run_job_check("proj_001", job, job["check_command"])
    assert client.finished == []
    assert client.progressed == [("j001", "RUNNING 10/100")]


def test_job_check_empty_output_is_silent() -> None:
    client = _FakeClient([])
    loop = _monitor_loop(client)
    job = {"id": "j001", "status": "running", "check_command": "true", "last_output": None}
    loop._run_job_check("proj_001", job, job["check_command"])
    assert client.finished == []
    assert client.progressed == []


def test_monitor_expires_job_over_budget() -> None:
    client = _FakeClient(
        [
            {
                "id": "j001",
                "status": "running",
                "check_command": "echo RUNNING",
                "budget_seconds": 60,
                "created_at": "2026-09-06T00:00:00Z",
                "last_output": "RUNNING 1/2",
            }
        ]
    )
    loop = _monitor_loop(client)
    loop._monitor_jobs([_summary("proj_001")])
    assert len(client.finished) == 1
    job_id, status, result = client.finished[0]
    assert job_id == "j001"
    assert status == "exhausted"
    assert "budget of 60s expired" in result


# ---------------------------------------------------------------- contracts


def test_explore_payload_accepts_jobs() -> None:
    payload = parse_json_output(
        '{"accepted": true, "data": {"description": "launched spray", '
        '"jobs": [{"description": "jake.h spray", "check_command": "grep -m1 HIT /tmp/spray.log", "budget_seconds": 3600}]}}'
    )
    kind, description, findings, jobs = validate_explore_payload(payload)
    assert kind == "fact"
    assert description == "launched spray"
    assert findings == []
    assert jobs == [
        {"description": "jake.h spray", "check_command": "grep -m1 HIT /tmp/spray.log", "budget_seconds": 3600}
    ]


def test_optional_jobs_rejects_missing_check_command() -> None:
    with pytest.raises(ValueError, match="check_command"):
        validate_optional_jobs([{"description": "no command"}])


def test_reason_payload_accepts_priority_and_blocked_by() -> None:
    payload = {
        "accepted": True,
        "data": {
            "intents": [
                {"from": ["f001"], "description": "escalate", "priority": 85, "blocked_by": "j001"},
            ]
        },
    }
    kind, data = validate_reason_payload(payload, open_intents_empty=False, max_intents=3)
    assert kind == "intents"
    assert data[0]["priority"] == 85
    assert data[0]["blocked_by"] == "j001"


def test_reason_payload_rejects_out_of_range_priority() -> None:
    payload = {"accepted": True, "data": {"intents": [{"from": ["f001"], "description": "x", "priority": 900}]}}
    with pytest.raises(ValueError, match="priority"):
        validate_reason_payload(payload, open_intents_empty=False, max_intents=3)


# ---------------------------------------------------------------- server API


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "board.db")
    with TestClient(app) as test_client:
        yield test_client


def _make_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={"title": "jobs-test", "origin": "origin", "goal": "goal"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def test_job_lifecycle_registers_monitors_and_synthesizes_fact(client: TestClient) -> None:
    project_id = _make_project(client)

    response = client.post(
        f"/projects/{project_id}/jobs",
        json={
            "description": "credential spray against jake.h",
            "check_command": "grep -m1 HIT /tmp/spray.log || true",
            "budget_seconds": 7200,
            "creator": "local-pi:i042",
        },
    )
    assert response.status_code == 201
    job = response.json()
    assert job["id"] == "j001"
    assert job["status"] == "running"

    response = client.post(
        f"/projects/{project_id}/jobs/j001/progress",
        json={"worker": "dispatcher.jobs", "output": "RUNNING 1000/37000000"},
    )
    assert response.status_code == 200
    assert response.json()["last_output"] == "RUNNING 1000/37000000"

    response = client.post(
        f"/projects/{project_id}/jobs/j001/finish",
        json={"worker": "dispatcher.jobs", "status": "hit", "result": "HIT jake.h : Winter#Storm42"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["job"]["status"] == "hit"
    assert body["job"]["fact_id"] == "f001"
    assert "jake.h : Winter#Storm42" in body["fact"]["description"]
    assert body["fact"]["description"].startswith("[job j001 · hit]")

    # double finish is rejected
    response = client.post(
        f"/projects/{project_id}/jobs/j001/finish",
        json={"worker": "dispatcher.jobs", "status": "done", "result": "again"},
    )
    assert response.status_code == 409

    # the synthetic fact reaches the graph export
    export = client.get(f"/projects/{project_id}/export").text
    assert "[job j001 · hit]" in export
    assert "jobs:" in export


def test_intent_priority_and_blocked_by_round_trip(client: TestClient) -> None:
    project_id = _make_project(client)
    client.post(
        f"/projects/{project_id}/jobs",
        json={"description": "spray", "check_command": "true", "budget_seconds": 600, "creator": "t"},
    )

    response = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "escalate after spray",
            "creator": "reasoner",
            "priority": 85,
            "blocked_by": "j001",
        },
    )
    assert response.status_code == 201
    intent = response.json()
    assert intent["priority"] == 85
    assert intent["blocked_by"] == "j001"

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["priority"] == 85
    assert detail["jobs"][0]["id"] == "j001"

    export = client.get(f"/projects/{project_id}/export").text
    assert "priority: 85" in export
    assert "blocked_by: j001" in export


def test_intent_priority_defaults_to_50_and_clamps(client: TestClient) -> None:
    project_id = _make_project(client)
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "plain", "creator": "reasoner"},
    )
    assert response.json()["priority"] == 50

    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "too big", "creator": "reasoner", "priority": 101},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- human overrides (API)


def test_intent_priority_update_sets_audit_fields(client: TestClient) -> None:
    project_id = _make_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "plain", "creator": "reasoner"},
    )

    response = client.put(
        f"/projects/{project_id}/intents/i001/priority",
        json={"priority": 95, "actor": "human"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["priority"] == 95
    assert body["priority_set_by"] == "human"
    assert body["priority_set_at"] is not None

    export = client.get(f"/projects/{project_id}/export").text
    assert "priority: 95" in export
    assert "priority_set_by: human" in export

    timeline = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert "PRIORITY SET i001 -> 95 by human" in timeline


def test_intent_priority_update_rejected_for_concluded(client: TestClient) -> None:
    project_id = _make_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "x", "creator": "r", "worker": "r"},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "r", "description": "done"},
    )
    response = client.put(
        f"/projects/{project_id}/intents/i001/priority",
        json={"priority": 90},
    )
    assert response.status_code == 409


def test_intent_priority_update_validates_range(client: TestClient) -> None:
    project_id = _make_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "x", "creator": "r"},
    )
    for bad in (0, 101, "high"):
        response = client.put(
            f"/projects/{project_id}/intents/i001/priority",
            json={"priority": bad},
        )
        assert response.status_code == 422


def test_intent_assignment_round_trip(client: TestClient) -> None:
    project_id = _make_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "x", "creator": "r"},
    )

    response = client.put(
        f"/projects/{project_id}/intents/i001/assign",
        json={"assigned_worker": "local-codex", "actor": "human"},
    )
    assert response.status_code == 200
    assert response.json()["assigned_worker"] == "local-codex"

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["assigned_worker"] == "local-codex"

    export = client.get(f"/projects/{project_id}/export").text
    assert "assigned_worker: local-codex" in export
    timeline = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert "assigned_to: local-codex" in timeline

    response = client.put(
        f"/projects/{project_id}/intents/i001/assign",
        json={"assigned_worker": None, "actor": "human"},
    )
    assert response.status_code == 200
    assert response.json()["assigned_worker"] is None


# ---------------------------------------------------------------- pin-aware dispatch


def _pin_loop(workers: list) -> DispatcherLoop:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(update={"workers": workers})
    loop.futures = {}
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(fact_count=3, hint_count=1, open_intent_count=1)
    loop.container_manager = type(
        "Containers", (), {"container_name": lambda _s, pid: pid, "worker_env": lambda _s, *_a, **_k: {}}
    )()
    claims: list[tuple[str, str]] = []

    class _ClaimClient:
        def get_project(self, _pid):
            return loop._pin_project

        def export_project(self, _pid):
            return "graph"

        def heartbeat(self, pid, iid, worker):
            claims.append((iid, worker))
            return type("R", (), {"ok": True, "status_code": 200, "text": ""})()

    loop.client = _ClaimClient()
    loop._claims = claims
    loop.executor = type(
        "Exec",
        (),
        {"submit": lambda _s, fn, *a, **k: Future()},
    )()
    return loop


def _two_workers():
    config = make_config()
    pi = config.workers[0].model_copy(update={"name": "local-pi", "priority": 0})
    codex = config.workers[0].model_copy(
        update={"name": "local-codex", "priority": 1, "task_types": ["explore"]}
    )
    return [pi, codex]


def test_pinned_intent_dispatches_to_assigned_worker() -> None:
    loop = _pin_loop(_two_workers())
    intent = make_intent("i001")
    intent.worker = None
    intent.priority = 50
    intent.assigned_worker = "local-codex"
    project = make_project(intents=[intent])
    loop._pin_project = project

    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i001", "local-codex")]


def test_pinned_intent_waits_when_assigned_worker_unknown() -> None:
    loop = _pin_loop(_two_workers())
    intent = make_intent("i001")
    intent.worker = None
    intent.assigned_worker = "ghost-worker"
    other = make_intent("i002")
    other.worker = None
    other.priority = 10
    project = make_project(intents=[intent, other])
    loop._pin_project = project
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(fact_count=3, hint_count=1, open_intent_count=2)

    assert loop._try_dispatch_project(_summary("proj_001"))
    # ghost-pinned intent skipped; the low-priority fallback dispatched instead
    assert loop._claims == [("i002", "local-pi")]


def test_pinned_intent_waits_when_assigned_worker_busy() -> None:
    from board.dispatcher.models import RunningTask
    from board.dispatcher.runtime.cancellation import TaskCancellation

    loop = _pin_loop(_two_workers())
    busy = Future()
    loop.futures[busy] = RunningTask("proj_001", "explore", "local-codex", TaskCancellation(), intent_id="i099")
    intent = make_intent("i001")
    intent.worker = None
    intent.priority = 95
    intent.assigned_worker = "local-codex"
    other = make_intent("i002")
    other.worker = None
    other.priority = 10
    project = make_project(intents=[intent, other])
    loop._pin_project = project
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(fact_count=3, hint_count=1, open_intent_count=2)

    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i002", "local-pi")]


# ---------------------------------------------------------------- kill + stop_command


def test_job_stop_command_round_trip(client: TestClient) -> None:
    project_id = _make_project(client)
    response = client.post(
        f"/projects/{project_id}/jobs",
        json={
            "description": "gobuster raft-medium",
            "check_command": "tail -1 /tmp/g.log",
            "stop_command": "pkill -f 'gobuster.*raft-medium' || true",
            "budget_seconds": 600,
            "creator": "local-pi:i001",
        },
    )
    assert response.status_code == 201
    assert response.json()["stop_command"] == "pkill -f 'gobuster.*raft-medium' || true"


def test_job_kill_transitions_and_writes_fact(client: TestClient) -> None:
    project_id = _make_project(client)
    client.post(
        f"/projects/{project_id}/jobs",
        json={"description": "spray", "check_command": "true", "budget_seconds": 600, "creator": "t"},
    )
    response = client.post(
        f"/projects/{project_id}/jobs/j001/kill",
        json={"actor": "human", "reason": "running too long"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["job"]["status"] == "killed"
    assert body["job"]["fact_id"] == "f001"
    assert "[job j001 · killed]" in body["fact"]["description"]
    assert "running too long" in body["fact"]["description"]
    assert "by human" in body["fact"]["description"]

    # double kill rejected
    response = client.post(f"/projects/{project_id}/jobs/j001/kill", json={})
    assert response.status_code == 409


def test_monitor_executes_stop_command_once_for_terminal_job(monkeypatch) -> None:
    stops: list[str] = []

    def fake_run(cmd, **kwargs):
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        stops.append(cmd)
        return P()

    monkeypatch.setattr("board.dispatcher.scheduler.loop.subprocess.run", fake_run)
    client = _FakeClient(
        [
            {
                "id": "j001",
                "status": "exhausted",
                "stop_command": "pkill -f gobuster || true",
                "check_command": "true",
                "created_at": "2026-09-06T00:00:00Z",
            }
        ]
    )
    loop = _monitor_loop(client)
    loop._monitor_jobs([_summary("proj_001")])
    loop._monitor_jobs([_summary("proj_001")])  # second tick must not re-run
    loop._monitor_jobs([_summary("proj_001")])
    assert stops == ["pkill -f gobuster || true"]


def test_monitor_exhaust_message_includes_last_check(monkeypatch) -> None:
    finished: list[tuple[str, str, str]] = []

    class _ExpiryClient(_FakeClient):
        def __init__(self, jobs):
            super().__init__(jobs)

        def finish_job(self, project_id, job_id, status, result):
            finished.append((job_id, status, result))
            return type("R", (), {"ok": True, "status_code": 200, "text": ""})()

    _ExpiryClient.__init__ = lambda self, jobs: _FakeClient.__init__(self, jobs)
    client = _ExpiryClient(
        [
            {
                "id": "j001",
                "status": "running",
                "check_command": "true",
                "budget_seconds": 60,
                "created_at": "2026-09-06T00:00:00Z",
                "last_check_at": "2026-09-06T00:30:00Z",
                "last_output": "RUNNING enum4linux still scanning",
            }
        ]
    )
    client.finished = finished
    loop = _monitor_loop(client)
    loop._monitor_jobs([_summary("proj_001")])
    assert len(finished) == 1
    job_id, status, result = finished[0]
    assert status == "exhausted"
    assert "last check 2026-09-06T00:30:00Z" in result
    assert "job ran" in result
    assert "RUNNING enum4linux still scanning" in result


def test_jobs_table_migration_adds_killed_status(tmp_path, monkeypatch) -> None:
    """A pre-kill jobs table (CHECK without 'killed') is rebuilt on configure."""
    import sqlite3
    from board.server import db as db_mod

    db_file = tmp_path / "old.db"
    conn = sqlite3.connect(db_file)
    conn.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            bootstrap_enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            reason_worker TEXT, reason_trigger TEXT, reason_started_at TEXT, reason_last_heartbeat_at TEXT,
            abort_reason TEXT, aborted_at TEXT, aborted_by TEXT);
        CREATE TABLE jobs (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','hit','done','exhausted','failed')),
            check_command TEXT,
            budget_seconds INTEGER,
            creator TEXT NOT NULL,
            last_check_at TEXT, last_output TEXT, fact_id TEXT,
            created_at TEXT NOT NULL, finished_at TEXT,
            PRIMARY KEY (id, project_id)
        );
        INSERT INTO projects VALUES ('p1','t','active',1,'2026-01-01T00:00:00Z',NULL,NULL,NULL,NULL,NULL,NULL,NULL);
        INSERT INTO jobs (id, project_id, description, status, creator, created_at)
            VALUES ('j001','p1','old job','exhausted','w','2026-01-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_mod, "_db_path", None)
    db_mod.configure(db_file)

    conn = sqlite3.connect(db_file)
    conn.execute(
        "UPDATE jobs SET status = 'killed' WHERE id = 'j001'"
    )  # would raise IntegrityError without rebuild
    row = conn.execute("SELECT status, description FROM jobs WHERE id = 'j001'").fetchone()
    assert row == ("killed", "old job")
    conn.close()
