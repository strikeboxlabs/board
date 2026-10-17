"""Lost-heartbeat task errors use synthetic processes and fake graph clients."""
from __future__ import annotations

import pytest

from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.runtime.heartbeat import HeartbeatFailure
from board.dispatcher.runtime.process import ProcessResult
from board.dispatcher.tasks import bootstrap, explore, reason
from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_intent, make_project


@pytest.mark.parametrize("task", [bootstrap, explore, reason])
def test_lost_heartbeat_has_actionable_task_failure_without_raw_response(monkeypatch, task):
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    monkeypatch.setattr(task, "get_driver", lambda *_: FakeDriver())
    factory = "for_reason" if task is reason else "for_intent"
    monkeypatch.setattr(task.HeartbeatLease, factory, lambda *_: lease)

    def process(*_args, **_kwargs):
        lease.failure = HeartbeatFailure(503, "RAW_RESPONSE_SECRET")
        return ProcessResult(143, "", "")

    monkeypatch.setattr(task, "run_worker_process", process)
    backend = FakeContainerManager()
    if task is bootstrap:
        outcome = task.run_bootstrap_task(config, client, backend, project, intent, config.workers[0], TaskCancellation())
    elif task is explore:
        outcome = task.run_explore_task(config, client, backend, project, "facts: []", intent, config.workers[0], TaskCancellation())
    else:
        outcome = task.run_reason_task(config, client, backend, project, "facts: []", config.workers[0], TaskCancellation())
    assert outcome == "failed" and "HTTP 503" in outcome.error
    assert "heartbeat renewal" in outcome.error and "database health" in outcome.error
    assert "RAW_RESPONSE_SECRET" not in outcome.error
    assert client.created_intents == [] and client.concluded == []
    assert lease.stopped
