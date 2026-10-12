from concurrent.futures import Future
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from board.dispatcher.models import RunningTask
from board.dispatcher.protocol.client import ApiResult
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.runtime.local_process import LocalProcess
from board.dispatcher.scheduler.intent_failures import IntentFailures
from board.dispatcher.tasks.common import TaskFailure
from board.server import db
from board.server.app import app
from conftest import make_config, make_intent, make_project
from test_jobs_and_priority import _pin_loop, _summary


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(tmp_path / 'board.db')
    with TestClient(app) as client:
        yield client


def setup_intent(client, claimed=False):
    pid = client.post('/projects', json={'title': 'Intent controls', 'origin': 'Local test',
        'goal': 'Verify lifecycle', 'bootstrap_enabled': False}).json()['project']['id']
    base = f'/projects/{pid}/intents'
    response = client.post(base, json={'from': ['origin'], 'description': 'Local test task',
        'creator': 'test-worker', 'worker': 'test-worker' if claimed else None})
    assert response.status_code == 201
    return pid, base + '/i001'


def test_failure_is_persistent_and_cannot_be_claimed_again(client):
    pid, url = setup_intent(client, claimed=True)
    response = client.post(url + '/fail', json={'worker': 'test-worker', 'error': 'CLI exited with code 1'})
    assert response.status_code == 200
    data = response.json()
    assert data['worker'] is None
    assert data['dispatch_error'] == 'CLI exited with code 1'
    assert data['dispatch_error_at']
    assert data['dispatch_error_worker'] == 'test-worker'
    assert client.post(url + '/heartbeat', json={'worker': 'test-worker'}).status_code == 409
    assert client.post(url + '/conclude', json={'worker': 'test-worker', 'description': 'Unexpected result'}).status_code == 409
    detail = client.get(f'/projects/{pid}').json()
    assert len(detail['facts']) == 2
    summary = client.get('/projects').json()[0]
    assert summary['working_intent_count'] == summary['unclaimed_intent_count'] == 0
    assert 'CLI exited with code 1' in client.get(f'/projects/{pid}/export').text


def test_queued_cancel_and_delete_preserve_facts_and_counter(client):
    pid, url = setup_intent(client)
    assert client.delete(url).status_code == 409
    cancelled = client.post(url + '/cancel', json={'actor': 'Human'}).json()
    assert cancelled['cancelled_at']
    assert cancelled['cancelled_by'] == 'Human'
    assert client.post(url + '/heartbeat', json={'worker': 'test-worker'}).status_code == 409
    assert client.delete(url).status_code == 204
    detail = client.get(f'/projects/{pid}').json()
    assert detail['intents'] == []
    assert [f['id'] for f in detail['facts']] == ['origin', 'goal']
    new = client.post(f'/projects/{pid}/intents', json={'from': ['origin'], 'description': 'Another task', 'creator': 'Human'})
    assert new.json()['id'] == 'i002'


def test_running_delete_waits_for_cancel_acknowledgement(client):
    pid, url = setup_intent(client, claimed=True)
    cancelled = client.post(url + '/cancel', json={'actor': 'Human'}).json()
    assert cancelled['cancel_requested_at']
    assert cancelled['cancelled_at'] is None
    assert cancelled['worker'] == 'test-worker'
    assert client.delete(url).status_code == 409
    # An expired heartbeat cannot be treated as proof that the process stopped.
    with db.get_conn() as conn:
        conn.execute("UPDATE intents SET last_heartbeat_at = '2000-01-01' WHERE id = 'i001'")
    assert client.get(f'/projects/{pid}').json()['intents'][0]['worker'] == 'test-worker'
    assert client.delete(url).status_code == 409
    ack = client.post(url + '/cancelled', json={'worker': 'test-worker'})
    assert ack.status_code == 200
    assert ack.json()['cancelled_at']
    assert ack.json()['worker'] is None
    assert client.delete(url).status_code == 204


def test_worker_release_after_cancel_acknowledges_stopped_process(client):
    _, url = setup_intent(client, claimed=True)
    client.post(url + '/cancel', json={'actor': 'Human'})
    released = client.post(url + '/release', json={'worker': 'test-worker'}).json()
    assert released['cancelled_at']
    assert client.delete(url).status_code == 204


def test_cancellation_wins_over_a_late_failure(client):
    _, url = setup_intent(client, claimed=True)
    client.post(url + '/cancel', json={'actor': 'Human'})
    result = client.post(url + '/fail', json={'worker': 'test-worker', 'error': 'Late exit'}).json()
    assert result['cancelled_at']
    assert result['dispatch_error'] is None


def test_another_worker_cannot_ack_or_fail_an_active_claim(client):
    _, url = setup_intent(client, claimed=True)
    assert client.post(url + '/fail', json={'worker': 'other', 'error': 'Failure'}).status_code == 409
    client.post(url + '/cancel', json={'actor': 'Human'})
    assert client.post(url + '/cancelled', json={'worker': 'other'}).status_code == 409


def test_concluded_intent_cannot_be_cancelled_or_deleted(client):
    _, url = setup_intent(client, claimed=True)
    assert client.post(url + '/conclude', json={'worker': 'test-worker', 'description': 'Recorded result'}).status_code == 200
    assert client.post(url + '/cancel', json={'actor': 'Human'}).status_code == 409
    assert client.delete(url).status_code == 409


def test_deletion_keeps_evidence_but_blocks_unstopped_background_jobs(client):
    pid, url = setup_intent(client)
    client.post(url + '/cancel', json={'actor': 'Human'})
    with db.get_conn() as conn:
        conn.execute("INSERT INTO evidence (id,project_id,intent_id,path,created_at) VALUES ('ev001',?,'i001','test.txt','2026-01-01')", (pid,))
        conn.execute("INSERT INTO jobs (id,project_id,description,status,creator,created_at) VALUES ('j001',?,'Local job','running','test-worker:i001','2026-01-01')", (pid,))
    assert client.delete(url).status_code == 409
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET status = 'done' WHERE id = 'j001'")
    assert client.delete(url).status_code == 204
    with db.get_conn() as conn:
        assert conn.execute('SELECT intent_id FROM evidence').fetchone()['intent_id'] is None


def test_failure_frees_slot_and_is_not_dispatched_again(tmp_path):
    loop = _pin_loop(make_config().workers)
    intent = make_intent()
    intent.worker = None
    loop._pin_project = make_project(intents=[intent])
    loop.intent_failures = IntentFailures(SimpleNamespace(reports_dir=lambda _: tmp_path))
    loop.client.fail_intent = lambda *_: ApiResult(False, 503, 'Offline')
    done = Future()
    done.set_result(TaskFailure('failed', 'Worker exited with code 1'))
    loop.futures[done] = RunningTask('proj_001', 'explore', 'test-worker', TaskCancellation(), intent_id='i001')
    loop._reap_futures()
    assert loop._worker_counts() == {}
    assert loop._intent_is_failed('proj_001', intent)
    loop._try_dispatch_project(_summary('proj_001'))
    assert loop._claims == []
    # The latch survives a dispatcher restart even if the API was unavailable.
    restored = IntentFailures(SimpleNamespace(reports_dir=lambda _: tmp_path))
    assert restored.state('proj_001')['i001']['error'] == 'Worker exited with code 1'


def test_cancel_does_not_ack_before_process_finishes():
    loop = _pin_loop(make_config().workers)
    intent = make_intent()
    intent.cancel_requested_at = '2026-10-12T12:00:00Z'
    loop._pin_project = make_project(intents=[intent])
    task = RunningTask('proj_001', 'explore', 'test-worker', TaskCancellation(), intent_id='i001')
    finished = Future()
    loop.futures[finished] = task
    acknowledgements = []
    loop.client.acknowledge_intent_cancel = lambda *args: acknowledgements.append(args) or ApiResult(True, 200, '')
    loop._cancel_requested_intents()
    assert task.cancellation.reason == 'intent_cancelled'
    assert acknowledgements == []
    assert loop._worker_counts() == {'test-worker': 1}
    finished.set_result('cancelled')
    loop._reap_futures()
    assert acknowledgements == [('proj_001', 'i001', 'test-worker')]
    assert loop._worker_counts() == {}


@pytest.mark.parametrize('leader_exits', [False, True])
def test_cancel_terminates_child_group_even_if_leader_exits(tmp_path, leader_exits):
    ready = tmp_path / 'ready'
    marker = tmp_path / 'should-not-exist'
    child = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(%r).touch(); time.sleep(1.0); Path(%r).touch()" % (str(ready), str(marker))
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); %s" % (child, 'pass' if leader_exits else 'time.sleep(10)')
    process = LocalProcess([sys.executable, '-c', parent], str(tmp_path), dict(os.environ), term_grace_seconds=1)
    process.start()
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists()
        if leader_exits:
            process._process.wait(timeout=2)
        process.cancel('intent_cancelled')
        result = process.communicate(timeout=2)
        assert result.cancelled
        time.sleep(1.1)
        assert not marker.exists()
    finally:
        process.kill()


def test_failed_worker_does_not_leave_a_child_process_running(tmp_path):
    ready = tmp_path / 'ready'
    marker = tmp_path / 'should-not-exist'
    child = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(%r).touch(); time.sleep(1.0); Path(%r).touch()" % (str(ready), str(marker))
    parent = "import subprocess,sys,time; from pathlib import Path; subprocess.Popen([sys.executable,'-c',%r]);\nwhile not Path(%r).exists(): time.sleep(.01)\nsys.exit(1)" % (child, str(ready))
    process = LocalProcess([sys.executable, '-c', parent], str(tmp_path), dict(os.environ), term_grace_seconds=1)
    process.start()
    try:
        result = process.communicate(timeout=3)
        assert result.returncode == 1
        time.sleep(1.1)
        assert not marker.exists()
    finally:
        process.kill()
