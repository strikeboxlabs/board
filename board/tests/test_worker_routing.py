from concurrent.futures import Future
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from board.dispatcher.models import ReasonCheckpoint, RunningTask
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.scheduler.reason_gate import graph_signature
from board.server import db
from board.server.app import app
from conftest import make_config, make_intent, make_project
from test_jobs_and_priority import _pin_loop, _summary


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "board.db")
    with TestClient(app) as client:
        yield client


def create_project(client):
    return client.post("/projects", json={
        "title": "Worker routing", "origin": "Test inputs", "goal": "Verify routing", "bootstrap_enabled": False,
    }).json()["project"]["id"]


@pytest.mark.parametrize("worker_type", ["pi", "codex", "claudecode"])
def test_worker_type_survives_create_reload_and_export(client, worker_type):
    pid = create_project(client)
    response = client.post(f"/projects/{pid}/intents", json={
        "from": ["origin"], "description": "Review code", "creator": "Human",
        "assigned_worker_type": worker_type,
    })
    assert response.status_code == 201
    assert response.json()["assigned_worker_type"] == worker_type
    assert response.json()["worker"] is None
    assert client.get(f"/projects/{pid}").json()["intents"][0]["assigned_worker_type"] == worker_type
    assert f"assigned_worker_type: {worker_type}" in client.get(f"/projects/{pid}/export").text
    assert f"worker_type: {worker_type}" in client.get(f"/projects/{pid}/export?format=timeline").text


def test_worker_type_edit_clear_and_legacy_pin_update(client):
    pid = create_project(client)
    url = f"/projects/{pid}/intents"
    client.post(url, json={"from": ["origin"], "description": "Review code", "creator": "Human"})
    assignment = url + "/i001/assign"
    assert client.put(assignment, json={"assigned_worker_type": "codex"}).json()["assigned_worker_type"] == "codex"
    legacy = client.put(assignment, json={"assigned_worker": "local-codex"}).json()
    assert legacy["assigned_worker_type"] == "codex"
    assert legacy["assigned_worker"] == "local-codex"
    cleared = client.put(assignment, json={"assigned_worker": None, "assigned_worker_type": None}).json()
    assert cleared["assigned_worker_type"] is None
    assert cleared["assigned_worker"] is None


def test_invalid_worker_type_is_rejected(client):
    pid = create_project(client)
    url = f"/projects/{pid}/intents"
    body = {"from": ["origin"], "description": "Review code", "creator": "Human"}
    assert client.post(url, json={**body, "assigned_worker_type": "unknown"}).status_code == 422
    client.post(url, json=body)
    assert client.put(url + "/i001/assign", json={"assigned_worker_type": "unknown"}).status_code == 422


def workers():
    template = make_config().workers[0]
    return [template.model_copy(update={"name": "local-" + kind, "type": kind,
            "priority": index, "task_types": ["explore"]})
            for index, kind in enumerate(("pi", "codex", "claudecode"))]


def setup_loop(worker_type, configured=None, other=False):
    loop = _pin_loop(workers() if configured is None else configured)
    intent = make_intent("i001")
    intent.worker = None
    intent.priority = 95
    intent.assigned_worker_type = worker_type
    intents = [intent]
    if other:
        fallback = make_intent("i002")
        fallback.worker = None
        fallback.priority = 10
        intents.append(fallback)
    loop._pin_project = make_project(intents=intents)
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(3, 1, len(intents))
    return loop


@pytest.mark.parametrize("worker_type", ["pi", "codex", "claudecode"])
def test_each_type_routes_to_matching_adapter(worker_type):
    loop = setup_loop(worker_type)
    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i001", "local-" + worker_type)]


@pytest.mark.parametrize("unavailable", ["busy", "unhealthy", "rejected", "unsupported", "missing"])
def test_unavailable_type_waits_and_other_intents_can_run(unavailable):
    configured = workers()
    if unavailable == "missing":
        configured = [w for w in configured if w.type != "codex"]
    elif unavailable == "unsupported":
        configured[1] = configured[1].model_copy(update={"task_types": ["reason"]})
    loop = setup_loop("codex", configured, other=True)
    if unavailable == "busy":
        loop.futures[Future()] = RunningTask("other-project", "explore", "local-codex", TaskCancellation())
    elif unavailable == "unhealthy":
        loop.worker_unhealthy_until["local-codex"] = time.time() + 60
    elif unavailable == "rejected":
        loop.worker_rejected_until[("proj_001", "explore", "local-codex")] = time.time() + 60
    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i002", "local-pi")]


def test_type_can_use_another_matching_worker_when_first_is_busy():
    configured = workers()
    configured.append(configured[1].model_copy(update={"name": "codex-second", "priority": 10}))
    loop = setup_loop("codex", configured)
    loop.futures[Future()] = RunningTask("other-project", "explore", "local-codex", TaskCancellation())
    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i001", "codex-second")]


def test_name_and_type_constraints_both_apply():
    loop = setup_loop("codex", other=True)
    loop._pin_project.intents[0].assigned_worker = "local-pi"
    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i002", "local-pi")]
    assert loop._select_worker("proj_001", "explore", "local-pi", "codex").worker is None


def test_routing_change_updates_planner_graph_signature():
    config = make_config()
    project = make_project(intents=[make_intent()])
    before = graph_signature(project, config)
    project.intents[0].assigned_worker_type = "codex"
    assert graph_signature(project, config) != before


def test_existing_database_migrates_without_changing_old_intents(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.SCHEMA.replace("    assigned_worker_type TEXT,\n", ""))
        conn.execute("INSERT INTO projects (id,title,created_at) VALUES ('p','Project','2026-01-01')")
        conn.execute("INSERT INTO intents (id,project_id,description,creator,created_at,assigned_worker) "
                     "VALUES ('i001','p','Existing work','Human','2026-01-01','local-pi')")
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        db._ensure_intent_columns(conn)
        row = conn.execute("SELECT * FROM intents").fetchone()
        assert row["description"] == "Existing work"
        assert row["assigned_worker"] == "local-pi"
        assert row["assigned_worker_type"] is None


def profile_loop(*, configured=None, other=False):
    configured = workers() if configured is None else configured
    configured = [w.model_copy(update={"priority": 99 if w.type == "pi" else w.priority}) for w in configured]
    loop = setup_loop(None, configured=configured, other=other)
    loop.config.runtime.execution = "local"
    loop.config.skills.enabled = True
    loop._pin_project.intents[0].skill_profiles = ["source-review"]
    loop._profile_failures = []
    def fail_intent(pid, iid, worker, error):
        loop._profile_failures.append((iid, worker, error))
        return type("Result", (), {"ok": True, "status_code": 200})()
    loop.client.fail_intent = fail_intent
    return loop


def test_profile_request_selects_pi_even_when_other_worker_has_higher_priority():
    loop = profile_loop()
    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i001", "local-pi")]
    assert loop._pin_project.intents[0].assigned_worker is None
    assert loop._pin_project.intents[0].assigned_worker_type is None
    assert loop._profile_failures == []


def test_profile_request_respects_explicit_pi_worker_pin():
    loop = profile_loop()
    loop._pin_project.intents[0].assigned_worker = "local-pi"
    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i001", "local-pi")]
    assert loop._pin_project.intents[0].assigned_worker == "local-pi"


def test_busy_profile_worker_does_not_block_an_unrelated_intent():
    loop = profile_loop(other=True)
    loop.futures[Future()] = RunningTask("other-project", "explore", "local-pi", TaskCancellation())
    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i002", "local-codex")]
    assert loop._profile_failures == []


def test_busy_profile_worker_waits_without_rerouting_or_failing():
    loop = profile_loop()
    loop.futures[Future()] = RunningTask("other-project", "explore", "local-pi", TaskCancellation())
    assert not loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == []
    assert loop._profile_failures == []


@pytest.mark.parametrize("incompatibility, expected", [
    ("disabled", "skills.enabled is false"),
    ("unknown", "Unknown skill profile"),
    ("nonlocal", "require local execution"),
    ("no-pi", "No Pi execution worker"),
    ("worker-pin", "human routing constraints"),
    ("type-pin", "human worker-type assignment"),
])
def test_incompatible_profile_request_halts_clearly_without_changing_human_routing(incompatibility, expected):
    loop = profile_loop()
    intent = loop._pin_project.intents[0]
    if incompatibility == "disabled":
        loop.config.skills.enabled = False
    elif incompatibility == "unknown":
        intent.skill_profiles = ["unknown-profile"]
    elif incompatibility == "nonlocal":
        loop.config.runtime.execution = "container"
    elif incompatibility == "no-pi":
        loop.config.workers = [w for w in loop.config.workers if w.type != "pi"]
    elif incompatibility == "worker-pin":
        intent.assigned_worker = "local-codex"
    elif incompatibility == "type-pin":
        intent.assigned_worker_type = "codex"
    human_routing = (intent.assigned_worker, intent.assigned_worker_type)
    assert not loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == []
    assert len(loop._profile_failures) == 1
    iid, worker, error = loop._profile_failures[0]
    assert iid == "i001" and worker is None
    assert expected in error and "Dispatch is stopped" in error
    assert (intent.assigned_worker, intent.assigned_worker_type) == human_routing


def test_direct_explore_dispatch_cannot_bypass_profile_compatibility():
    loop = profile_loop()
    loop._pin_project.intents[0].assigned_worker_type = "codex"
    assert not loop._dispatch_explore(loop._pin_project, "graph", loop._pin_project.intents[0])
    assert loop._claims == []
    assert "human worker-type assignment" in loop._profile_failures[0][2]


@pytest.mark.parametrize("human_edit, expected_worker", [
    ({"skill_profiles": ["python"]}, "local-pi"),
    ({"skill_profiles": [], "assigned_worker": "local-codex"}, "local-codex"),
    ({"skill_profiles": [], "assigned_worker_type": "codex"}, "local-codex"),
])
def test_requirements_changed_between_fetch_and_claim_defer_without_launch_or_halt(human_edit, expected_worker):
    loop = profile_loop()
    original_heartbeat = loop.client.heartbeat
    edited = False
    def heartbeat(pid, iid, worker):
        nonlocal edited
        if not edited:
            # A completed human edit after the dispatcher's snapshot but before its claim.
            loop._pin_project.intents[0] = loop._pin_project.intents[0].model_copy(update=human_edit, deep=True)
            edited = True
        return original_heartbeat(pid, iid, worker)
    loop.client.heartbeat = heartbeat
    assert not loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims == [("i001", "local-pi")]
    assert loop._released_claims == [("i001", "local-pi")]
    assert loop.futures == {}
    assert loop._profile_failures == []
    assert loop._try_dispatch_project(_summary("proj_001"))
    assert loop._claims[-1] == ("i001", expected_worker)
    assert len(loop.futures) == 1
    assert loop._profile_failures == []
