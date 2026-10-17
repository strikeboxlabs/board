"""A sanitized heartbeat cause survives scheduler, API and error-history storage."""
from concurrent.futures import Future
from types import SimpleNamespace

from fastapi.testclient import TestClient

from board.dispatcher.models import RunningTask
from board.dispatcher.protocol.client import ApiResult
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.runtime.heartbeat import HeartbeatFailure
from board.dispatcher.scheduler.intent_failures import IntentFailures
from board.dispatcher.tasks.common import TaskFailure
from board.server import db
from board.server.app import app
from board.server.models import ProjectDetail
from conftest import make_config
from test_jobs_and_priority import _pin_loop


def test_heartbeat_cause_is_visible_and_persistent_without_raw_response(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "reporting.db")
    private_body = "PRIVATE_SERVER_BODY_MUST_NOT_BE_STORED"
    failure = HeartbeatFailure(503, private_body)
    outcome = TaskFailure("failed", f"Worker test-worker: {failure.safe_message}")
    assert "heartbeat" in outcome.error.lower() and "503" in outcome.error
    assert private_body not in outcome.error

    with TestClient(app) as api:
        pid = api.post("/projects", json={
            "title": "Heartbeat reporting fixture", "origin": "Local fixture",
            "goal": "Verify failure reporting", "bootstrap_enabled": False,
        }).json()["project"]["id"]
        response = api.post(f"/projects/{pid}/intents", json={
            "from": ["origin"], "description": "Inspect local fixture",
            "creator": "test-worker", "worker": "test-worker",
        })
        assert response.status_code == 201
        iid = response.json()["id"]
        loop = _pin_loop(make_config().workers)
        loop._pin_project = ProjectDetail.model_validate(api.get(f"/projects/{pid}").json())
        loop.intent_failures = IntentFailures(SimpleNamespace(reports_dir=lambda _: tmp_path / "reports"))

        def persist_failure(project_id, intent_id, worker, error):
            result = api.post(f"/projects/{project_id}/intents/{intent_id}/fail",
                              json={"worker": worker, "error": error})
            return ApiResult(result.status_code, result.json(), result.text)

        loop.client.fail_intent = persist_failure
        done = Future()
        done.set_result(outcome)
        loop.futures[done] = RunningTask(pid, "explore", "test-worker", TaskCancellation(), intent_id=iid)
        loop._reap_futures()

        detail = api.get(f"/projects/{pid}").json()
        assert detail["intents"][0]["dispatch_error"] == outcome.error
        assert detail["intents"][0]["worker"] is None
        assert loop._worker_counts() == {}
        assert outcome.error in api.get(f"/projects/{pid}/export?format=timeline").text
        assert api.post(f"/projects/{pid}/intents/{iid}/heartbeat",
                        json={"worker": "test-worker"}).status_code == 409
        restored = IntentFailures(SimpleNamespace(reports_dir=lambda _: tmp_path / "reports"))
        assert restored.state(pid)[iid]["error"] == outcome.error
        assert private_body not in str(detail)
