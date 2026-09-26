from concurrent.futures import Future
from types import SimpleNamespace
import sqlite3

import pytest
from fastapi.testclient import TestClient

from board.server import db
from board.server.app import app
from board.dispatcher.scheduler.loop import DispatcherLoop
from board.dispatcher.prompting import with_execution_constraints
from board.dispatcher.protocol.client import ApiResult
from board.server.models import ProjectSummary
from conftest import make_project, make_intent, make_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(tmp_path / 'suspend.db')
    with TestClient(app) as client:
        yield client


def setup_project(client):
    pid = client.post('/projects', json={'title':'Suspend test', 'origin':'start', 'goal':'finish'}).json()['project']['id']
    intent = client.post(f'/projects/{pid}/intents', json={'from':['origin'], 'description':'enumeration', 'creator':'worker', 'worker':'worker'}).json()
    client.post(f'/projects/{pid}/reason/claim', json={'worker':'reasoner', 'trigger':'test'})
    return pid, intent['id']


def suspend(client, pid):
    response = client.put(f'/projects/{pid}/status', json={'status':'stopped'})
    assert response.status_code == 200
    return response.json()['suspension_id']


def acknowledge(client, pid, token):
    assert client.post(f'/projects/{pid}/suspension/ack', json={'suspension_id':token}).status_code == 200


def test_suspend_preserves_progress_and_requires_explicit_resume_after_ack(client, monkeypatch):
    pid, iid = setup_project(client)
    token = suspend(client, pid)
    snapshot = client.get(f'/projects/{pid}').json()
    assert snapshot['project']['reason'] is None
    assert snapshot['intents'][0]['worker'] is None
    assert snapshot['intents'][0]['to'] is None
    assert len(snapshot['facts']) == 2
    assert client.put(f'/projects/{pid}/status', json={'status':'active'}).status_code == 409
    assert client.post(f'/projects/{pid}/intents/{iid}/heartbeat', json={'worker':'worker'}).status_code == 403
    # Reinitializing the process preserves suspension, with no timer to expire.
    path = db._db_path
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(path)
    assert client.get(f'/projects/{pid}').json()['project']['status'] == 'stopped'
    acknowledge(client, pid, token)
    assert client.get(f'/projects/{pid}').json()['project']['status'] == 'stopped'
    resumed = client.put(f'/projects/{pid}/status', json={'status':'active', 'execution_notes':'Do not repeat enumeration.', 'excluded_intent_ids':[iid]})
    assert resumed.status_code == 200
    assert resumed.json()['execution_notes'] == 'Do not repeat enumeration.'
    assert client.post(f'/projects/{pid}/intents/{iid}/heartbeat', json={'worker':'worker'}).status_code == 409
    assert client.post(f'/projects/{pid}/intents/{iid}/conclude', json={'worker':'worker', 'description':'late result'}).status_code == 409
    detail = client.get(f'/projects/{pid}').json()
    assert detail['intents'][0]['excluded'] is True
    assert detail['facts'] == snapshot['facts']
    assert client.get('/projects').json()[0]['unclaimed_intent_count'] == 0
    assert 'excluded: true' in client.get(f'/projects/{pid}/export').text
    token2 = suspend(client, pid)
    assert token2 != token
    assert client.post(f'/projects/{pid}/suspension/ack', json={'suspension_id':token}).status_code == 409
    acknowledge(client, pid, token2)
    assert client.put(f'/projects/{pid}/status', json={'status':'active'}).status_code == 200
    assert client.get(f'/projects/{pid}').json()['intents'][0]['excluded'] is True


def test_job_cleanup_and_resume_are_atomic(client):
    pid, iid = setup_project(client)
    job = client.post(f'/projects/{pid}/jobs', json={'description':'background task', 'creator':'worker', 'stop_command':'example-stop'}).json()
    manual = client.post(f'/projects/{pid}/jobs', json={'description':'external task', 'creator':'worker'}).json()
    assert client.get(f'/projects/{pid}/jobs').json()[0]['stop_command'] == 'example-stop'
    token = suspend(client, pid)
    acknowledge(client, pid, token)
    jobs = client.get(f'/projects/{pid}/jobs').json()
    assert [(j['status'], j['stop_state']) for j in jobs] == [('killed','pending'), ('killed','manual')]
    base = f'/projects/{pid}'
    # Confirming the manual job while another is pending must not partially save.
    assert client.put(base+'/status', json={'status':'active','confirmed_stopped_job_ids':[manual['id']]}).status_code == 409
    assert client.get(base+'/jobs').json()[1]['stop_state'] == 'manual'
    assert client.post(base+f"/jobs/{job['id']}/stop-result", json={'success':False,'error':'cannot stop'}).status_code == 200
    assert client.put(base+'/status', json={'status':'active'}).status_code == 409
    assert client.put(base+'/status', json={'status':'active','confirmed_stopped_job_ids':[job['id'],manual['id']], 'excluded_intent_ids':['missing']}).status_code == 409
    assert client.get(base+'/jobs').json()[0]['stop_state'] == 'failed'
    assert client.put(base+'/status', json={'status':'active','confirmed_stopped_job_ids':[job['id'],manual['id']], 'excluded_intent_ids':[iid]}).status_code == 200
    assert all(j['stop_state'] == 'stopped' for j in client.get(base+'/jobs').json())
    assert client.get(base).json()['project']['status'] == 'active'


def test_suspension_is_idempotent_and_terminal_projects_stay_terminal(client):
    pid, _ = setup_project(client)
    token = suspend(client, pid)
    assert suspend(client, pid) == token
    client.post(f'/projects/{pid}/abort', json={'reason':'test'})
    assert client.put(f'/projects/{pid}/status', json={'status':'active'}).status_code == 409
    assert client.put(f'/projects/{pid}/status', json={'status':'stopped'}).status_code == 409


def test_excluded_intent_is_never_dispatched_or_counted(monkeypatch):
    from test_jobs_and_priority import _loop, _summary
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    excluded = make_intent('i001'); excluded.excluded = True; excluded.worker = None
    runnable = make_intent('i002'); runnable.worker = None
    project = make_project(intents=[excluded, runnable])
    loop.client = SimpleNamespace(get_project=lambda _:project, export_project=lambda _:'graph')
    loop.container_manager = SimpleNamespace(container_name=lambda pid:pid)
    selected = []
    monkeypatch.setattr(loop, '_reason_trigger', lambda *_:None)
    monkeypatch.setattr(loop, '_dispatch_explore', lambda p,y,i:selected.append(i.id) or True)
    assert loop._try_dispatch_project(_summary('proj_001'))
    assert selected == ['i002']
    assert loop._project_open_intent_count(project) == 1
    project.project.execution_notes = 'No high-volume requests.'
    prompt = with_execution_constraints('ORIGINAL', project)
    assert 'No high-volume requests.' in prompt and 'i001' in prompt and prompt.endswith('ORIGINAL')
    excluded.description = 'bootstrap'; excluded.creator = 'dispatcher.bootstrap'; excluded.from_ = ['origin']
    project.intents = [excluded]
    assert not loop._project_requires_bootstrap(project)


class ImmediateExecutor:
    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future


def test_stopped_project_executes_cleanup_only_and_retries_ack_not_command(monkeypatch):
    from test_jobs_and_priority import _loop, _summary
    loop = _loop()
    loop.cleanup_executor = ImmediateExecutor()
    calls = []
    job = {'id':'j001','status':'killed','stop_state':'pending','stop_command':'safe-stop','check_command':'never-run'}
    acks = []
    def ack(*args):
        acks.append(args)
        if len(acks) == 1:
            return ApiResult(status_code=503, text='offline')
        job['stop_state'] = 'stopped'
        return ApiResult(status_code=200, text='ok')
    loop.client = SimpleNamespace(list_jobs=lambda _:[job], record_job_stop=ack)
    monkeypatch.setattr('board.dispatcher.scheduler.loop.subprocess.run', lambda cmd,**kw:calls.append(cmd) or SimpleNamespace(returncode=0,stdout='',stderr=''))
    for _ in range(5):
        loop._monitor_jobs([_summary('proj_001','stopped')])
    assert calls == ['safe-stop']
    assert len(acks) == 2
    # A dispatcher restart sees the durable acknowledgement and does not rerun stop.
    loop._job_stop_futures = {}; loop._job_stop_results = {}
    loop._monitor_jobs([_summary('proj_001','stopped')])
    assert calls == ['safe-stop']


def test_worker_ack_waits_for_processes_checks_and_container_cleanup():
    from test_jobs_and_priority import _loop, _summary
    loop = _loop(); summary = _summary('proj_001','stopped'); summary.suspension_id = 'token'
    calls = []; loop.client = SimpleNamespace(acknowledge_suspension=lambda *a:calls.append(a))
    loop.futures = {1:SimpleNamespace(project_id='proj_001')}
    loop._acknowledge_suspensions([summary]); assert not calls
    loop.futures = {}; loop._job_checks_inflight = {('proj_001','j001')}
    loop._acknowledge_suspensions([summary]); assert not calls
    loop._job_checks_inflight = set()
    loop._acknowledge_suspensions([summary]); assert not calls
    loop._inactive_cleanup_done['proj_001'] = 'stopped'
    loop._acknowledge_suspensions([summary]); assert calls == [('proj_001','token')]


def test_legacy_stopped_projects_gain_cleanup_barrier_without_losing_data(client, monkeypatch):
    pid, iid = setup_project(client)
    client.post(f'/projects/{pid}/jobs', json={'description':'legacy job','creator':'worker'})
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status = 'stopped' WHERE id = ?", (pid,))
    path = db._db_path
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(path)
    detail = client.get(f'/projects/{pid}').json()
    assert detail['project']['suspension_id']
    assert detail['jobs'][0]['stop_state'] == 'manual'
    assert detail['intents'][0]['id'] == iid
    assert len(detail['facts']) == 2
