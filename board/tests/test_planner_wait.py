from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from board.planning import planner_input_signature
from board.server import db
from board.server.app import app
from board.server.models import ProjectDetail


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "waiting.db")
    with TestClient(app) as client:
        yield client


def project(client):
    return client.post("/projects", json={"title": "Waiting assessment", "origin": "source", "goal": "Find supported defects"}).json()["project"]["id"]


def claim(client, pid, worker="planner"):
    return client.post(f"/projects/{pid}/reason/claim", json={"worker": worker, "trigger": "test"}).json()["reason"]["lease_id"]


def wait_payload(client, pid, lease_id):
    snapshot = ProjectDetail.model_validate(client.get(f"/projects/{pid}").json())
    return {"worker": "planner", "lease_id": lease_id, "reason": "Need scope guidance <script>never execute</script>",
            "input_signature": planner_input_signature(snapshot)}


def write_wait(client, pid):
    token = claim(client, pid)
    payload = wait_payload(client, pid, token)
    assert client.post(f"/projects/{pid}/reason/wait", json=payload).status_code == 200
    assert client.post(f"/projects/{pid}/reason/release", json={"worker": "planner"}).status_code == 200
    return payload


def test_wait_survives_release_restart_and_is_in_detail_and_list(client, monkeypatch):
    pid = project(client)
    payload = write_wait(client, pid)
    expected = client.get(f"/projects/{pid}").json()["project"]["planner_wait"]
    assert expected["reason"] == payload["reason"]
    assert expected["since"] and expected["worker"] == "planner"
    path = db._db_path
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    detail = client.get(f"/projects/{pid}").json()["project"]
    assert detail["status"] == "active" and detail["reason"] is None
    assert detail["planner_wait"] == expected
    assert client.get("/projects").json()[0]["planner_wait"] == expected


def test_stale_wait_cannot_overwrite_new_guidance_or_new_lease(client):
    pid = project(client)
    token = claim(client, pid)
    payload = wait_payload(client, pid, token)
    client.post(f"/projects/{pid}/hints", json={"content": "Review existing evidence", "creator": "human"})
    assert client.post(f"/projects/{pid}/reason/wait", json=payload).status_code == 409
    payload = wait_payload(client, pid, token)
    client.post(f"/projects/{pid}/reason/release", json={"worker": "planner"})
    assert claim(client, pid) != token  # even the same worker in the same second
    assert client.post(f"/projects/{pid}/reason/wait", json=payload).status_code == 409
    assert client.get(f"/projects/{pid}").json()["project"]["planner_wait"] is None


def test_old_same_worker_lease_cannot_release_or_heartbeat_new_lease(client):
    pid = project(client)
    old = claim(client, pid)
    client.post(f"/projects/{pid}/reason/release", json={"worker": "planner", "lease_id": old})
    new = claim(client, pid)
    for operation in ("release", "heartbeat"):
        assert client.post(f"/projects/{pid}/reason/{operation}", json={"worker": "planner", "lease_id": old}).status_code == 409
        assert client.get(f"/projects/{pid}").json()["project"]["reason"]["lease_id"] == new
    assert client.post(f"/projects/{pid}/reason/heartbeat", json={"worker": "planner", "lease_id": new}).status_code == 200
    assert client.post(f"/projects/{pid}/reason/release", json={"worker": "planner", "lease_id": new}).status_code == 200


def test_no_op_input_updates_preserve_wait_across_trigger_upgrade(client, monkeypatch):
    pid = project(client)
    iid = client.post(f"/projects/{pid}/intents", json={"from": ["origin"], "description": "Work", "creator": "human"}).json()["id"]
    write_wait(client, pid)
    expected = client.get(f"/projects/{pid}").json()["project"]["planner_wait"]
    with db.get_conn() as conn:
        conn.execute("DROP TRIGGER clear_planner_wait_intents_update")
        conn.execute("""CREATE TRIGGER clear_planner_wait_intents_update AFTER UPDATE OF priority ON intents
            BEGIN UPDATE projects SET planner_wait = NULL WHERE id = NEW.project_id; END""")
    path = db._db_path
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    assert client.put(f"/projects/{pid}/intents/{iid}/priority", json={"priority": 50}).status_code == 200
    assert client.put(f"/projects/{pid}/intents/{iid}/assign", json={"assigned_worker": None, "assigned_worker_type": None, "skill_profiles": []}).status_code == 200
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status = status, execution_notes = execution_notes WHERE id = ?", (pid,))
        conn.execute("UPDATE facts SET description = description WHERE project_id = ?", (pid,))
    assert client.get(f"/projects/{pid}").json()["project"]["planner_wait"] == expected
    assert client.put(f"/projects/{pid}/intents/{iid}/priority", json={"priority": 51}).status_code == 200
    assert client.get(f"/projects/{pid}").json()["project"]["planner_wait"] is None


@pytest.mark.parametrize("change", ["hint", "intent", "fact", "policy", "job"])
def test_new_inputs_clear_wait(client, change):
    pid = project(client)
    write_wait(client, pid)
    if change == "hint":
        client.post(f"/projects/{pid}/hints", json={"content": "New scope", "creator": "human"})
    elif change == "intent":
        client.post(f"/projects/{pid}/intents", json={"from": ["origin"], "description": "New work", "creator": "human"})
    elif change == "fact":
        with db.get_conn() as conn:
            conn.execute("INSERT INTO facts (id, project_id, description) VALUES ('f001', ?, 'New evidence')", (pid,))
    elif change == "policy":
        with db.get_conn() as conn:
            conn.execute("UPDATE projects SET execution_notes = 'Use current evidence' WHERE id = ?", (pid,))
    else:
        client.post(f"/projects/{pid}/jobs", json={"description": "Background work", "creator": "human"})
    assert client.get(f"/projects/{pid}").json()["project"]["planner_wait"] is None


def test_heartbeats_preserve_wait_but_next_planner_claim_clears_it(client):
    pid = project(client)
    iid = client.post(f"/projects/{pid}/intents", json={"from": ["origin"], "description": "Work", "creator": "human"}).json()["id"]
    write_wait(client, pid)
    client.post(f"/projects/{pid}/intents/{iid}/heartbeat", json={"worker": "worker"})
    assert client.get(f"/projects/{pid}").json()["project"]["planner_wait"] is not None
    claim(client, pid)
    assert client.get(f"/projects/{pid}").json()["project"]["planner_wait"] is None


def test_conclusion_preserves_uncertainty_reports_and_reopen(client, monkeypatch, tmp_path):
    monkeypatch.setenv("BOARD_PROJECTS_DIR", str(tmp_path / "reports"))
    pid = project(client)
    write_wait(client, pid)
    response = client.post(f"/projects/{pid}/complete", json={"from": ["origin"], "description": "Scope exhausted; coverage gaps remain", "worker": "human", "outcome": "current_results"})
    assert response.status_code == 200
    meta = client.get(f"/projects/{pid}").json()["project"]
    assert meta["status"] == "completed" and meta["completion_outcome"] == "current_results"
    assert meta["planner_wait"] is None
    human = client.get(f"/projects/{pid}/report?type=human").text
    assert "original goal is not asserted to have been achieved" in human
    agent = client.get(f"/projects/{pid}/report?type=agent-json").json()
    assert agent["header"]["completion_outcome"] == "current_results"
    timeline = client.get(f"/projects/{pid}/export?format=timeline").text
    assert "PROJECT CONCLUDED WITH CURRENT RESULTS" in timeline
    assert client.post(f"/projects/{pid}/reopen", json={"description": "Additional scope", "creator": "human"}).status_code == 200
    assert client.get(f"/projects/{pid}").json()["project"]["completion_outcome"] is None


def test_current_results_rejects_open_work_and_active_planner(client):
    pid = project(client)
    body = {"from": ["origin"], "description": "Conclude", "worker": "human", "outcome": "current_results"}
    claim(client, pid)
    assert client.post(f"/projects/{pid}/complete", json=body).status_code == 409
    client.post(f"/projects/{pid}/reason/release", json={"worker": "planner"})
    client.post(f"/projects/{pid}/intents", json={"from": ["origin"], "description": "Work", "creator": "human"})
    assert client.post(f"/projects/{pid}/complete", json=body).status_code == 409


@pytest.mark.parametrize("status", ["killed", "exhausted"])
@pytest.mark.parametrize("stop_state", ["pending", "manual", "failed"])
def test_current_results_requires_background_stop_ack(client, status, stop_state):
    pid = project(client)
    jid = client.post(f"/projects/{pid}/jobs", json={"description": "Background work", "creator": "human"}).json()["id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET status = ?, stop_state = ? WHERE project_id = ? AND id = ?", (status, stop_state, pid, jid))
    body = {"from": ["origin"], "description": "Conclude", "worker": "human", "outcome": "current_results"}
    response = client.post(f"/projects/{pid}/complete", json=body)
    assert response.status_code == 409 and "Confirm background jobs" in response.json()["detail"]
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET stop_state = 'stopped' WHERE project_id = ? AND id = ?", (pid, jid))
    assert client.post(f"/projects/{pid}/complete", json=body).status_code == 200


def test_replay_does_not_leak_final_outcome_and_ui_requires_stop_ack():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the UI model")
    path = Path(__file__).parents[1] / "src/board/server/static/index.html"
    source = "function boardApp() {" + path.read_text().split("function boardApp() {", 1)[1].split("</script>", 1)[0]
    checks = r"""
const assert = require('node:assert/strict');
function tacticalConsole() { return {}; }
function liveFindingsView() { return {}; }
function suspensionControls() { return {}; }
const app = boardApp();
const final = {project: {status: 'completed', completion_outcome: 'current_results', planner_wait: {reason: 'later'}},
 facts: [{id:'origin'}, {id:'goal'}], hints: [], intents: [{id:'i001', to:'goal', from:['origin']}]};
const initial = app.buildInitialReplayProject(final);
assert.equal(initial.project.completion_outcome, null);
assert.equal(initial.project.planner_wait, null);
assert.equal(initial.project.status, 'active');
app.applyReplayEvent(initial, final, {type:'project_completed', intentId:'i001'});
assert.equal(initial.project.completion_outcome, 'current_results');
app.project = {project:{status:'active'}, intents:[], jobs:[{status:'killed',stop_state:'pending'}]};
assert.equal(app.canConcludeCurrentResults(), false);
for (const state of ['manual','failed']) { app.project.jobs[0].stop_state=state; assert.equal(app.canConcludeCurrentResults(), false); }
app.project.jobs[0].stop_state='stopped';
assert.equal(app.canConcludeCurrentResults(), true);
"""
    result = subprocess.run([node], input=source + checks, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_ordinary_completion_still_records_goal_met(client):
    pid = project(client)
    assert client.post(f"/projects/{pid}/complete", json={"from": ["origin"], "description": "Goal supported", "worker": "human"}).status_code == 200
    assert client.get(f"/projects/{pid}").json()["project"]["completion_outcome"] == "goal_met"
