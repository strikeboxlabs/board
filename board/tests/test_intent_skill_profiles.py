from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient
import pytest
import yaml

from board.dispatcher.contracts import validate_reason_payload
from board.dispatcher.protocol.client import ApiResult, BoardClient
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.runtime.process import ProcessResult
from board.dispatcher.scheduler.reason_gate import graph_signature
from board.dispatcher.tasks import reason
from board.server import db
from board.server.app import app
from board.skill_profiles import validate_skill_profile_ids
from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_intent, make_project


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "profiles.db")
    with TestClient(app) as client:
        yield client


def project_id(client):
    response = client.post("/projects", json={
        "title": "Source review", "origin": "Local fixture", "goal": "Review source", "bootstrap_enabled": False,
    })
    assert response.status_code == 201
    return response.json()["project"]["id"]


def create_intent(client, pid, **extra):
    return client.post(f"/projects/{pid}/intents", json={
        "from": ["origin"], "description": "Inspect the local source fixture", "creator": "Human", **extra,
    })


def test_profiles_survive_api_reload_edits_and_exports(client):
    pid = project_id(client)
    profiles = ["source-analysis", "python"]
    response = create_intent(client, pid, skill_profiles=profiles, assigned_worker="local-pi", assigned_worker_type="pi")
    assert response.status_code == 201
    intent = response.json()
    assert intent["skill_profiles"] == profiles
    assert intent["assigned_worker"] == "local-pi"
    assert intent["assigned_worker_type"] == "pi"
    assert client.get(f"/projects/{pid}").json()["intents"][0]["skill_profiles"] == profiles
    exported = yaml.safe_load(client.get(f"/projects/{pid}/export").text)
    assert exported["intents"][0]["skill_profiles"] == profiles
    assert "skill_profiles: source-analysis, python" in client.get(f"/projects/{pid}/export?format=timeline").text

    url = f"/projects/{pid}/intents/{intent['id']}/skill-profiles"
    changed = client.put(url, json={"skill_profiles": ["local-regression"]})
    assert changed.status_code == 200
    assert changed.json()["skill_profiles"] == ["local-regression"]
    assert changed.json()["assigned_worker"] == "local-pi"
    assert client.put(url, json={"skill_profiles": []}).json()["skill_profiles"] == []
    assert "skill_profiles" not in yaml.safe_load(client.get(f"/projects/{pid}/export").text)["intents"][0]


def test_old_clients_default_to_no_requested_profiles_and_server_does_not_resolve_ids(client):
    pid = project_id(client)
    assert create_intent(client, pid).json()["skill_profiles"] == []
    # Profile availability belongs to the dispatcher host, not the graph server.
    assert create_intent(client, pid, skill_profiles=["not-installed-on-this-server"]).status_code == 201


@pytest.mark.parametrize("profiles", [
    None, "source-analysis", [""], ["../source"], ["Source"], ["source_analysis"],
    [" source"], ["-source"], ["source-"], ["source--analysis"], ["a" * 65],
    [1], [True], ["source", "source"], [f"p-{i}" for i in range(17)],
])
def test_invalid_profile_requests_are_rejected_by_api_and_reason_contract(client, profiles):
    pid = project_id(client)
    assert create_intent(client, pid, skill_profiles=profiles).status_code == 422
    created = create_intent(client, pid).json()
    assert client.put(f"/projects/{pid}/intents/{created['id']}/skill-profiles", json={"skill_profiles": profiles}).status_code == 422
    with pytest.raises(ValueError):
        validate_reason_payload({"accepted": True, "data": {"intents": [
            {"from": ["origin"], "description": "Review local source", "skill_profiles": profiles},
        ]}}, False, 1)


def test_profile_id_boundaries_preserve_requested_order():
    profiles = ["a" * 64] + [f"p-{i}" for i in range(15)]
    assert validate_skill_profile_ids(profiles) == profiles
    assert validate_skill_profile_ids(profiles) is not profiles


def test_profiles_cannot_change_during_run_or_after_conclusion_or_cancellation(client):
    pid = project_id(client)
    intent = create_intent(client, pid, skill_profiles=["source-analysis"]).json()
    base = f"/projects/{pid}/intents/{intent['id']}"
    assert client.post(base + "/heartbeat", json={"worker": "local-pi"}).status_code == 200
    assert client.put(base + "/skill-profiles", json={"skill_profiles": ["python"]}).status_code == 409
    assert client.post(base + "/conclude", json={"worker": "local-pi", "description": "Reviewed source"}).status_code == 200
    assert client.put(base + "/skill-profiles", json={"skill_profiles": []}).status_code == 409

    cancelled = create_intent(client, pid).json()
    cancel_base = f"/projects/{pid}/intents/{cancelled['id']}"
    assert client.post(cancel_base + "/cancel", json={"actor": "Human"}).status_code == 200
    assert client.put(cancel_base + "/skill-profiles", json={"skill_profiles": ["python"]}).status_code == 409


def test_existing_database_migration_defaults_profiles_and_preserves_routing(tmp_path, monkeypatch):
    path = tmp_path / "legacy-profiles.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.SCHEMA.replace("    skill_profiles TEXT NOT NULL DEFAULT '[]',\n", ""))
        conn.execute("INSERT INTO projects (id,title,created_at) VALUES ('p','Project','2026-01-01')")
        conn.execute("INSERT INTO intents (id,project_id,description,creator,created_at,assigned_worker,assigned_worker_type) "
                     "VALUES ('i001','p','Existing work','Human','2026-01-01','local-pi','pi')")
    conn.close()
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        db._ensure_intent_columns(conn)
        row = conn.execute("SELECT * FROM intents WHERE id='i001'").fetchone()
        assert json.loads(row["skill_profiles"]) == []
        assert row["description"] == "Existing work"
        assert row["assigned_worker"] == "local-pi"
        assert row["assigned_worker_type"] == "pi"


def test_profile_change_invalidates_planner_graph_signature():
    config = make_config()
    project = make_project(intents=[make_intent()])
    before = graph_signature(project, config)
    project.intents[0].skill_profiles = ["source-analysis"]
    assert graph_signature(project, config) != before


def test_protocol_client_preserves_requested_profiles(monkeypatch):
    client = BoardClient("http://unused")
    calls = []
    monkeypatch.setattr(client, "_request_json", lambda *args, **kwargs: calls.append((args, kwargs)) or ApiResult(201, {}))
    client.create_intent("p", ["origin"], "Inspect local fixture", "reasoner", skill_profiles=["source-analysis"])
    assert calls[0][1]["json"]["skill_profiles"] == ["source-analysis"]
    client.close()


def test_reason_forwards_profile_requests_without_changing_worker_routing(monkeypatch):
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *_a, **_k: lease)
    monkeypatch.setattr(reason, "run_worker_process", lambda *_a, **_k: ProcessResult(0, json.dumps({
        "accepted": True, "data": {"intents": [{"from": ["f001"], "description": "Inspect local fixture", "skill_profiles": ["source-analysis"]}]},
    }), ""))
    outcome = reason.run_reason_task(config, client, containers, project, "facts: []", config.workers[0], TaskCancellation())
    assert outcome == "success"
    assert client.requested_skill_profiles == [["source-analysis"]]
    assert client.created_intents == [("proj_001", ["f001"], "Inspect local fixture", "test-worker")]
    assert "{skill_profiles}" not in driver.execute_prompts[0]


def test_atomic_profile_and_assignment_edits_support_both_transition_directions(client):
    pid = project_id(client)
    intent = create_intent(client, pid, assigned_worker="local-codex", assigned_worker_type="codex").json()
    assignment = f"/projects/{pid}/intents/{intent['id']}/assign"
    to_pi = client.put(assignment, json={"assigned_worker": "local-pi", "assigned_worker_type": "pi", "skill_profiles": ["source-analysis"]})
    assert to_pi.status_code == 200
    assert (to_pi.json()["assigned_worker"], to_pi.json()["assigned_worker_type"], to_pi.json()["skill_profiles"]) == ("local-pi", "pi", ["source-analysis"])
    to_codex = client.put(assignment, json={"assigned_worker": "local-codex", "assigned_worker_type": "codex", "skill_profiles": []})
    assert to_codex.status_code == 200
    assert (to_codex.json()["assigned_worker"], to_codex.json()["assigned_worker_type"], to_codex.json()["skill_profiles"]) == ("local-codex", "codex", [])


def test_assignment_omission_preserves_profiles_but_explicit_empty_clears_them(client):
    pid = project_id(client)
    intent = create_intent(client, pid, skill_profiles=["source-analysis"], assigned_worker_type="pi").json()
    assignment = f"/projects/{pid}/intents/{intent['id']}/assign"
    assert client.put(assignment, json={"assigned_worker": "local-pi"}).json()["skill_profiles"] == ["source-analysis"]
    assert client.put(assignment, json={"assigned_worker": "local-pi", "skill_profiles": []}).json()["skill_profiles"] == []


@pytest.mark.parametrize("updates", [
    {"assigned_worker": "changed", "assigned_worker_type": "unsupported", "skill_profiles": []},
    {"assigned_worker": "changed", "assigned_worker_type": "pi", "skill_profiles": ["../bad"]},
    {"assigned_worker": "changed", "assigned_worker_type": "pi", "skill_profiles": None},
])
def test_bad_atomic_edit_leaves_assignment_and_profiles_unchanged(client, updates):
    pid = project_id(client)
    intent = create_intent(client, pid, assigned_worker="local-pi", assigned_worker_type="pi", skill_profiles=["source-analysis"]).json()
    assignment = f"/projects/{pid}/intents/{intent['id']}/assign"
    assert client.put(assignment, json=updates).status_code == 422
    current = client.get(f"/projects/{pid}").json()["intents"][0]
    assert (current["assigned_worker"], current["assigned_worker_type"], current["skill_profiles"]) == ("local-pi", "pi", ["source-analysis"])


def test_running_atomic_profile_edit_rolls_back_assignment_but_legacy_routing_edit_remains_available(client):
    pid = project_id(client)
    intent = create_intent(client, pid, assigned_worker="local-pi", assigned_worker_type="pi", skill_profiles=["source-analysis"]).json()
    base = f"/projects/{pid}/intents/{intent['id']}"
    assert client.post(base + "/heartbeat", json={"worker": "local-pi"}).status_code == 200
    assert client.put(base + "/assign", json={"assigned_worker": "changed", "assigned_worker_type": "codex", "skill_profiles": []}).status_code == 409
    current = client.get(f"/projects/{pid}").json()["intents"][0]
    assert (current["assigned_worker"], current["assigned_worker_type"], current["skill_profiles"]) == ("local-pi", "pi", ["source-analysis"])
    legacy = client.put(base + "/assign", json={"assigned_worker": "local-pi"})
    assert legacy.status_code == 200
    assert legacy.json()["skill_profiles"] == ["source-analysis"]
