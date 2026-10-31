from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from fastapi.testclient import TestClient
import requests

from board.dispatcher.scheduler.loop import DispatcherLoop
from board.server import db
from board.server.app import app
from board.server.models import DispatchOverrides, Settings, SettingsSnapshot
from conftest import make_config


@pytest.fixture
def loop(tmp_path):
    obj = DispatcherLoop.__new__(DispatcherLoop)
    obj.config_path = tmp_path / 'dispatch.yaml'
    obj.config = make_config()
    obj.config_path.write_text(yaml.safe_dump(obj.config.model_dump()))
    obj._file_config = obj.config
    obj._config_stamp = obj._file_stamp()
    obj._file_revision = obj._revision_for(obj._config_stamp)
    obj._file_error = None
    obj._reload_error = None
    obj._server_settings = None
    obj._settings_checked = False
    obj._applied_overrides = DispatchOverrides()
    obj._routing = {}
    obj._retired_executors = []
    obj._last_effective_report = None
    obj._last_effective_report_at = 0
    obj._log_state = {}
    obj.futures = {}
    obj.executor = ThreadPoolExecutor(max_workers=2)
    obj.reason_gate = SimpleNamespace(config=obj.config.tasks.reason)
    obj.client = Mock()
    obj.client.get_settings.return_value = Settings(intent_timeout=150, reason_timeout=150, file_revision=obj._file_revision)
    yield obj
    obj.executor.shutdown()
    for executor in obj._retired_executors:
        executor.shutdown()


def rewrite(loop, edit):
    data = yaml.safe_load(loop.config_path.read_text())
    edit(data)
    loop.config_path.write_text(yaml.safe_dump(data))


def test_reload_resize_preserves_running_snapshot(loop):
    original = loop.config
    done = Event()
    future = loop.executor.submit(lambda: done.wait(5))
    loop.futures[future] = None
    try:
        rewrite(loop, lambda d: d['runtime'].update(max_workers=4))
        loop._refresh_config()
        assert loop.config.runtime.max_workers == 4
        assert original.runtime.max_workers == 2
        assert not future.done()
        rewrite(loop, lambda d: d['runtime'].update(max_workers=2))
        loop._refresh_config()
        assert loop.config.runtime.max_workers == 2
        assert not future.done()
    finally:
        done.set()


def test_file_change_while_settings_api_is_unavailable_discards_older_overrides(loop):
    loop.client.get_settings.return_value.dispatch.reason_execution_timeout = 42
    loop._refresh_config()
    loop.client.get_settings.side_effect = requests.ConnectionError("offline")
    rewrite(loop, lambda d: d['workers'][0].update(max_running=2))
    loop._refresh_config()
    assert loop.config.workers[0].max_running == 2
    assert loop.config.tasks.reason.timeout == 10
    assert 'Settings poll failed' in loop._reload_error


def test_effective_report_failure_does_not_block_reload(loop):
    loop.client.report_effective.side_effect = requests.ConnectionError("offline")
    rewrite(loop, lambda d: d['tasks']['explore'].update(timeout=30))
    loop._refresh_config()
    assert loop.config.tasks.explore.timeout == 30


def test_reports_changes_immediately_and_only_periodic_liveness_when_unchanged(loop, monkeypatch):
    monkeypatch.setattr('board.dispatcher.scheduler.loop.time.monotonic', lambda: 1)
    loop._refresh_config()
    loop._refresh_config()
    assert loop.client.report_effective.call_count == 1
    assert loop.client.get_settings.call_count == 2
    monkeypatch.setattr('board.dispatcher.scheduler.loop.time.monotonic', lambda: 32)
    loop._refresh_config()
    assert loop.client.report_effective.call_count == 2
    loop.client.get_settings.return_value.dispatch.reason_execution_timeout = 42
    loop._refresh_config()
    assert loop.client.report_effective.call_count == 3


def test_invalid_reload_diagnostics_exclude_input_secrets(loop, caplog):
    rewrite(loop, lambda d: d['workers'][0].update(max_running='secret-not-a-number'))
    loop._refresh_config()
    assert 'workers.0.max_running' in caplog.text
    assert 'secret-not-a-number' not in caplog.text
    assert 'secret-not-a-number' not in str(loop.client.report_effective.call_args)


def test_missing_file_keeps_config_and_recovers(loop):
    contents = loop.config_path.read_text()
    loop.config_path.unlink()
    loop._refresh_config()
    assert loop.config == loop._file_config
    assert loop._reload_error
    loop.config_path.write_text(contents)
    loop._refresh_config()
    assert loop._reload_error is None


def test_interval_reload_cannot_outgrow_heartbeat_leases(loop):
    rewrite(loop, lambda d: d['runtime'].update(interval=200))
    loop._refresh_config()
    assert loop.config.runtime.interval == 60
    assert 'heartbeat leases' in loop._reload_error


@pytest.mark.parametrize('content', ['tasks: [', 'runtime: {}'])
def test_bad_file_retains_config_and_recovers(loop, content):
    valid = loop.config_path.read_text()
    loop.config_path.write_text(content)
    loop._refresh_config()
    assert loop.config == loop._file_config
    assert loop._reload_error
    loop.config_path.write_text(valid)
    loop._refresh_config()
    assert loop._reload_error is None


def test_overrides_precedence_reset_and_pin(loop):
    template = loop.config.workers[0].model_dump()
    rewrite(loop, lambda d: d['workers'].append({**template, 'name': 'standby-worker', 'task_types': []}))
    loop._refresh_config()
    settings = loop.client.get_settings.return_value
    settings.file_revision = loop._file_revision
    settings.dispatch.reason_execution_timeout = 42
    settings.dispatch.routing = {'explore': ['standby-worker']}
    settings.dispatch.worker_max_running = {'test-worker': 2}
    loop._refresh_config()
    assert loop.config.tasks.reason.timeout == 42
    assert loop.config.workers[0].max_running == 2
    assert not loop._route_allows(loop.config.workers[0], 'explore', False)
    assert loop._route_allows(loop.config.workers[0], 'explore', True)
    assert loop._file_config.tasks.reason.timeout == 10
    settings.dispatch = type(settings.dispatch)()
    loop._refresh_config()
    assert loop.config.tasks.reason.timeout == 10
    assert loop.config.workers[0].max_running == 1
    payload = loop.client.report_effective.call_args.args[0]
    assert 'env' not in payload['workers'][0]
    assert payload['routing']['explore'] == ['test-worker']


@pytest.mark.parametrize('pin', ['name', 'type', None])
def test_live_routing_controls_real_worker_selection_and_preserves_pins(pin):
    from test_worker_routing import setup_loop

    obj = setup_loop('codex' if pin == 'type' else None)
    obj._routing = {'explore': ['local-pi']}
    if pin == 'name':
        obj._pin_project.intents[0].assigned_worker = 'local-codex'
    assert obj._dispatch_explore(obj._pin_project, 'graph', obj._pin_project.intents[0])
    assert obj._claims == [('i001', 'local-codex' if pin else 'local-pi')]


def test_lower_global_limit_waits_for_existing_tasks(loop):
    loop.futures = {Mock(): None for _ in range(3)}
    loop._try_dispatch_project = Mock()
    loop._dispatch_available([])
    loop._try_dispatch_project.assert_not_called()


@pytest.mark.parametrize('pinned', [False, True])
def test_skill_requirements_respect_live_routing_unless_human_pinned(pinned):
    from test_worker_routing import profile_loop
    from test_jobs_and_priority import _summary

    obj = profile_loop()
    obj._routing = {'explore': ['local-codex']}
    if pinned:
        obj._pin_project.intents[0].assigned_worker = 'local-pi'
    assert obj._try_dispatch_project(_summary('proj_001')) == pinned
    assert obj._claims == ([('i001', 'local-pi')] if pinned else [])
    assert obj._profile_failures == []


def test_loop_polls_once_and_applies_file_before_dispatch(loop):
    loop.run_startup_healthchecks = Mock()
    loop.close = Mock()
    for name in ('_reap_futures', '_reap_cleanup_futures', '_initialize_reason_checkpoints',
                 '_refresh_runtime_projects', '_cancel_inactive_tasks', '_cancel_requested_intents',
                 '_queue_container_cleanups', '_persist_terminal_reports', '_monitor_jobs',
                 '_acknowledge_suspensions'):
        setattr(loop, name, Mock())
    loop.client.list_projects.return_value = []
    observed_timeouts = []
    loop._dispatch_available = lambda _: observed_timeouts.append(loop.config.tasks.reason.timeout)
    rewrite(loop, lambda d: d['tasks']['reason'].update(timeout=30))
    loop.run(once=True)
    assert observed_timeouts == [30]
    assert loop.client.get_settings.call_count == 1
    loop.close.assert_called_once()


def test_restart_only_changes_rejected(loop):
    rewrite(loop, lambda d: d.update(server='http://different'))
    loop._refresh_config()
    assert loop.config.server == 'http://127.0.0.1:8000'
    assert loop._reload_error


def test_unknown_override_keeps_last_effective(loop):
    loop._refresh_config()
    previous = loop.config
    loop.client.get_settings.return_value.dispatch.worker_max_running = {'missing': 1}
    loop._refresh_config()
    assert loop.config is previous
    assert loop._reload_error


def test_live_routing_can_assign_tasks_outside_file_defaults(loop):
    rewrite(loop, lambda d: d['workers'][0].update(task_types=['reason']))
    loop._refresh_config()
    loop.client.get_settings.return_value.file_revision = loop._file_revision
    loop.client.get_settings.return_value.dispatch.routing = {'explore': ['test-worker']}
    loop._refresh_config()
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    assert loop._reload_error is None
    assert loop._select_worker('proj_001', 'explore').worker.name == 'test-worker'
    assert loop._worker_available('proj_001', None, 'explore')
    assert loop.client.report_effective.call_args.args[0]['routing']['explore'] == ['test-worker']


def test_unassigned_worker_is_available_to_enable_without_changing_default_routing(loop):
    template = loop.config.workers[0].model_dump()
    new_worker = {**template, 'name': 'standby-worker', 'task_types': []}
    rewrite(loop, lambda d: d['workers'].append(new_worker))
    loop._refresh_config()
    loop.client.get_settings.return_value.file_revision = loop._file_revision
    standby = next(w for w in loop.config.workers if w.name == 'standby-worker')
    assert not loop._route_allows(standby, 'reason', False)
    loop.client.get_settings.return_value.dispatch.routing = {'reason': ['standby-worker']}
    loop._refresh_config()
    assert loop._route_allows(standby, 'reason', False)
    assert not loop._route_allows(loop.config.workers[0], 'reason', False)


def test_live_bootstrap_route_is_used_when_file_only_assigns_reason(loop):
    from conftest import make_project
    rewrite(loop, lambda d: d['workers'][0].update(task_types=['reason']))
    loop._refresh_config()
    loop.client.get_settings.return_value.file_revision = loop._file_revision
    project = make_project()
    assert not loop._project_requires_bootstrap(project)
    loop.client.get_settings.return_value.dispatch.routing = {'bootstrap': ['test-worker']}
    loop._refresh_config()
    assert loop._project_requires_bootstrap(project)


def report_context(client, config=None, **changes):
    config = config or make_config()
    config.runtime.interval = 3
    public = DispatcherLoop._public_config(config)
    payload = {**public, 'routing': {}, 'file_defaults': public, 'applied_overrides': {}, **changes}
    response = client.put('/settings/effective', json=payload)
    assert response.status_code == 200
    return response.json()


def test_settings_persist_validate_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    path = tmp_path / 'board.db'
    db.configure(path)
    with TestClient(app) as client:
        assert client.get('/settings/effective').json() is None
        report_context(client)
        settings = client.get('/settings').json()
        settings['dispatch'] = {'routing': {'reason': ['test-worker']}, 'worker_max_running': {'test-worker': 2}}
        assert client.put('/settings', json=settings).status_code == 200
        monkeypatch.setattr(db, '_db_path', None)
        db.configure(path)
        assert client.get('/settings').json()['dispatch']['routing'] == {'reason': ['test-worker']}
        settings['dispatch']['worker_max_running']['test-worker'] = 0
        assert client.put('/settings', json=settings).status_code == 422
        payload = {'runtime': {}, 'tasks': {}, 'workers': [], 'routing': {},
                   'file_defaults': {}, 'applied_overrides': {}}
        assert client.put('/settings/effective', json=payload).status_code == 200
        assert client.get('/settings/effective').json()['reported_at']


@pytest.mark.parametrize('override', [
    {'worker_max_running': {'w': True}},
    {'worker_max_running': {'w': 1.5}},
    {'worker_max_running': {'w': -1}},
    {'reason_execution_timeout': 0},
    {'explore_execution_timeout': -1},
    {'routing': {'unknown': ['w']}},
    {'routing': {'reason': ['w', 'w']}},
])
def test_invalid_overrides_rejected(override):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(intent_timeout=15, reason_timeout=15, dispatch=override)


def test_legacy_settings_migrate_and_legacy_updates_preserve_overrides(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE settings (intent_timeout INTEGER, reason_timeout INTEGER)')
        conn.execute('INSERT INTO settings VALUES (30, 45)')
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(path)
    with TestClient(app) as client:
        settings = client.get('/settings').json()
        assert settings['intent_timeout'] == 30
        assert settings['dispatch'] == DispatchOverrides().model_dump()
        settings['dispatch'] = {'reason_execution_timeout': 42}
        assert client.put('/settings', json=settings).status_code == 200
        updated = client.put('/settings', json={'intent_timeout': 35, 'reason_timeout': 50}).json()
        assert updated['dispatch']['reason_execution_timeout'] == 42
        cleared = client.put('/settings', json={**updated, 'dispatch': {}}).json()
        assert cleared['dispatch'] == DispatchOverrides().model_dump()


@pytest.fixture
def settings_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(tmp_path / 'validation.db')
    with TestClient(app) as client:
        report_context(client)
        yield client


@pytest.mark.parametrize('override', [
    {'routing': {'reason': ['test-worker', 'other-worker']}},
    {'routing': {'reason': []}},
    {'routing': {'explore': ['unknown-worker']}},
    {'worker_max_running': {'test-worker': 3}},  # Global limit is two.
    {'reason_execution_timeout': 60000},
    {'explore_execution_timeout': 600000000},
])
def test_api_rejects_invalid_settings_without_changing_saved_values(settings_client, override):
    before = settings_client.get('/settings').json()
    response = settings_client.put('/settings', json={**before, 'dispatch': override})
    assert response.status_code == 422
    assert settings_client.get('/settings').json() == before


def test_unavailable_model_rejected_before_save(settings_client):
    config = make_config()
    public = DispatcherLoop._public_config(config)
    public['runtime']['interval'] = 3
    public['workers'][0].update(available=False, unavailable_reason='CLI is not installed')
    response = settings_client.put('/settings/effective', json={
        **public, 'routing': {}, 'file_defaults': public, 'applied_overrides': {},
    })
    assert response.status_code == 200
    before = settings_client.get('/settings').json()
    response = settings_client.put('/settings', json={
        **before, 'dispatch': {'routing': {'reason': ['test-worker']}},
    })
    assert response.status_code == 422
    assert 'unavailable' in response.json()['detail']
    assert settings_client.get('/settings').json() == before


def test_heartbeat_leases_respect_running_workers_interval(settings_client):
    report_context(settings_client, heartbeat_interval=10)
    body = settings_client.get('/settings').json()
    response = settings_client.put('/settings', json={**body, 'intent_timeout': 5})
    assert response.status_code == 422
    assert 'heartbeat interval' in response.json()['detail']
    assert settings_client.get('/settings').json()['intent_timeout'] == 15


def test_stale_report_cannot_authorize_worker_changes(settings_client):
    import json
    with db.get_conn() as conn:
        report = json.loads(conn.execute('SELECT payload FROM dispatch_effective').fetchone()[0])
        report['reported_at'] = '2000-01-01T00:00:00+00:00'
        conn.execute('UPDATE dispatch_effective SET payload = ?', (json.dumps(report),))
    body = settings_client.get('/settings').json()
    response = settings_client.put('/settings', json={**body, 'dispatch': {'routing': {'reason': ['test-worker']}}})
    assert response.status_code == 422
    assert 'stale' in response.json()['detail']


def test_legacy_joke_settings_remain_readable_and_can_be_reset(settings_client):
    import json
    joke = {'routing': {'reason': ['test-worker', 'other-worker']},
            'worker_max_running': {'test-worker': 32},
            'reason_execution_timeout': 60000, 'explore_execution_timeout': 600000000}
    with db.get_conn() as conn:
        conn.execute('UPDATE settings SET dispatch = ?', (json.dumps(joke),))
    response = settings_client.get('/settings')
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot['validation_errors']
    assert snapshot['dispatch']['explore_execution_timeout'] == 600000000
    assert snapshot['limits']['worker_max_running'] == 2
    reset = settings_client.put('/settings', json={**snapshot, 'dispatch': {}})
    assert reset.status_code == 200
    assert reset.json()['validation_errors'] == []
    assert reset.json()['dispatch'] == DispatchOverrides().model_dump()


def test_dispatcher_rejects_old_joke_values_and_retains_last_valid_snapshot(loop):
    loop.client.get_settings.return_value.dispatch.reason_execution_timeout = 42
    loop._refresh_config()
    previous = loop.config
    loop.client.get_settings.return_value = SettingsSnapshot(
        intent_timeout=150, reason_timeout=150, file_revision=loop._file_revision,
        dispatch={'routing': {'reason': ['test-worker', 'other-worker']},
                  'worker_max_running': {'test-worker': 32},
                  'reason_execution_timeout': 60000, 'explore_execution_timeout': 600000000},
    )
    loop._refresh_config()
    assert loop.config is previous
    assert loop.config.tasks.reason.timeout == 42
    assert loop._reload_error
    assert loop.client.report_effective.call_args.args[0]['applied_overrides']['reason_execution_timeout'] == 42


def test_initial_invalid_saved_settings_fall_back_to_file_defaults(loop):
    loop.client.get_settings.return_value = SettingsSnapshot(
        intent_timeout=150, reason_timeout=150, file_revision=loop._file_revision,
        dispatch={'explore_execution_timeout': 600000000},
    )
    loop._refresh_config()
    assert loop.config.tasks.explore.timeout == 10
    assert loop._reload_error
    assert loop.client.report_effective.call_args.args[0]['applied_overrides'] == DispatchOverrides().model_dump()


def test_global_limit_shrink_clears_older_worker_overrides(loop):
    rewrite(loop, lambda d: d['runtime'].update(max_workers=4))
    loop._refresh_config()
    loop.client.get_settings.return_value.file_revision = loop._file_revision
    loop.client.get_settings.return_value.dispatch.worker_max_running = {'test-worker': 4}
    loop._refresh_config()
    assert loop.config.workers[0].max_running == 4
    rewrite(loop, lambda d: d['runtime'].update(max_workers=2))
    loop._refresh_config()
    assert loop.config.runtime.max_workers == 2
    assert loop.config.workers[0].max_running == 1
    assert loop._reload_error is None


def test_missing_local_cli_is_reported_unavailable(loop, monkeypatch):
    config = loop.config.model_copy(deep=True)
    config.runtime.execution = 'local'
    config.workers[0].type = 'codex'
    monkeypatch.setattr('board.dispatcher.scheduler.loop.shutil.which', lambda _: None)
    public = DispatcherLoop._public_config(config)
    assert public['workers'][0]['available'] is False
    assert 'codex' in public['workers'][0]['unavailable_reason']
    loop.config = config
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    assert loop._select_worker('proj_001', 'reason').worker is None


@pytest.mark.parametrize('change', [
    lambda d: d['tasks']['reason'].update(timeout=60000),
    lambda d: d['tasks']['explore'].update(timeout=600000000),
    lambda d: d['workers'][0].update(max_running=32),
    lambda d: d['workers'].append({**d['workers'][0], 'name': 'duplicate-role'}),
])
def test_invalid_file_policy_does_not_replace_working_configuration(loop, change):
    previous = loop.config
    rewrite(loop, change)
    loop._refresh_config()
    assert loop.config is previous
    assert loop._reload_error


@pytest.fixture
def synchronized_loop(loop, tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(tmp_path / 'source.db')
    with TestClient(app) as http:
        assert http.put('/settings', json={'intent_timeout': 150, 'reason_timeout': 150}).status_code == 200

        class ApiClient:
            def get_settings(self):
                response = http.get('/settings')
                response.raise_for_status()
                return SettingsSnapshot.model_validate(response.json())

            def report_effective(self, payload):
                response = http.put('/settings/effective', json=payload)
                assert response.status_code == 200, response.text

        loop.client = ApiClient()
        loop._refresh_config()
        yield loop, http


def save_reason_override(http, timeout=42):
    body = http.get('/settings').json()
    response = http.put('/settings', json={**body, 'dispatch': {'reason_execution_timeout': timeout}})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('restore', ['in_place', 'atomic', 'preserve_mtime'])
def test_restoring_backup_clears_old_web_controls_everywhere(synchronized_loop, restore):
    import os
    obj, http = synchronized_loop
    original = obj.config_path.read_text()
    stat = obj.config_path.stat()
    old = save_reason_override(http)
    obj._refresh_config()
    assert obj.config.tasks.reason.timeout == 42

    if restore == 'atomic':
        replacement = obj.config_path.with_name('replacement.yaml')
        replacement.write_text(original)
        replacement.replace(obj.config_path)
    else:
        obj.config_path.write_text(original)
        if restore == 'preserve_mtime':
            os.utime(obj.config_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    obj._refresh_config()
    current = http.get('/settings').json()
    assert obj.config.tasks.reason.timeout == 10
    assert obj._reload_error is None
    assert current['dispatch'] == DispatchOverrides().model_dump()
    assert current['validation_errors'] == []
    assert current['file_revision'] != old['file_revision']
    assert http.get('/settings/effective').json()['tasks']['reason']['timeout'] == 10
    # A stale form cannot reapply its old controls after the restore.
    response = http.put('/settings', json=old)
    assert response.status_code == 409
    assert http.get('/settings').json() == current


def test_invalid_disk_edit_does_not_clear_saved_or_effective_controls(synchronized_loop):
    obj, http = synchronized_loop
    old = save_reason_override(http)
    obj._refresh_config()
    obj.config_path.write_text('tasks: [')
    obj._refresh_config()
    assert obj.config.tasks.reason.timeout == 42
    assert obj._file_error
    assert http.get('/settings').json()['dispatch'] == old['dispatch']
    assert http.get('/settings').json()['file_revision'] == old['file_revision']


def restart_config_state(obj):
    from board.dispatcher.config import DispatchConfig
    obj.config = DispatchConfig.load(obj.config_path)
    obj._file_config = obj.config
    obj._config_stamp = obj._file_stamp()
    obj._file_revision = obj._revision_for(obj._config_stamp)
    obj._applied_overrides = DispatchOverrides()
    obj._routing = {}
    obj._server_settings = None
    obj._settings_checked = False
    obj._last_effective_report = None


def test_unchanged_file_and_restart_preserve_new_web_settings(synchronized_loop):
    obj, http = synchronized_loop
    saved = save_reason_override(http)
    obj._refresh_config()
    restart_config_state(obj)
    obj._refresh_config()
    assert obj.config.tasks.reason.timeout == 42
    assert http.get('/settings').json()['dispatch'] == saved['dispatch']
    assert obj._file_revision == saved['file_revision']


def test_restore_while_dispatcher_is_down_is_detected_at_startup(synchronized_loop):
    obj, http = synchronized_loop
    save_reason_override(http)
    obj.config_path.write_text(obj.config_path.read_text())
    restart_config_state(obj)
    obj._refresh_config()
    assert obj.config.tasks.reason.timeout == 10
    assert http.get('/settings').json()['dispatch'] == DispatchOverrides().model_dump()


def test_legacy_unscoped_joke_values_are_cleared_when_file_is_registered(settings_client):
    import json
    with db.get_conn() as conn:
        conn.execute('UPDATE settings SET dispatch = ?', (json.dumps({'explore_execution_timeout': 600000000}),))
    report_context(settings_client, file_revision='restored-file')
    snapshot = settings_client.get('/settings').json()
    assert snapshot['dispatch'] == DispatchOverrides().model_dump()
    assert snapshot['validation_errors'] == []
    assert snapshot['file_revision'] == 'restored-file'


def test_saved_controls_and_file_revision_are_read_in_one_snapshot(settings_client, monkeypatch):
    import board.server.routers.settings as router
    report_context(settings_client, file_revision='old-file')
    saved = save_reason_override(settings_client)
    entered, release = Event(), Event()
    original = router._read_settings

    def paused(conn):
        raw = original(conn)
        entered.set()
        assert release.wait(5)
        return raw

    monkeypatch.setattr(router, '_read_settings', paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(settings_client.get, '/settings')
        try:
            assert entered.wait(5)
            report_context(settings_client, file_revision='restored-file')
        finally:
            release.set()
        snapshot = future.result(timeout=5).json()
    assert snapshot['file_revision'] == 'old-file'
    assert snapshot['dispatch'] == saved['dispatch']
    current = settings_client.get('/settings').json()
    assert current['file_revision'] == 'restored-file'
    assert current['dispatch'] == DispatchOverrides().model_dump()


def test_live_worker_changes_require_the_file_revision(settings_client):
    report_context(settings_client, file_revision='current-file')
    response = settings_client.put('/settings', json={
        'intent_timeout': 15, 'reason_timeout': 15, 'dispatch': {'reason_execution_timeout': 42},
    })
    assert response.status_code == 409
    # Legacy heartbeat-only clients remain supported.
    assert settings_client.put('/settings', json={'intent_timeout': 30, 'reason_timeout': 45}).status_code == 200


def test_file_revision_matches_the_validated_bytes_during_concurrent_edit(loop, monkeypatch):
    from board.dispatcher.config import DispatchConfig
    original = DispatchConfig.load
    calls = []

    def changing_file(path):
        config = original(path)
        calls.append(True)
        if len(calls) == 1:
            rewrite(loop, lambda d: d['tasks']['reason'].update(timeout=20))
        return config

    monkeypatch.setattr(DispatchConfig, 'load', staticmethod(changing_file))
    config, stamp = loop._load_config_snapshot()
    assert len(calls) == 2
    assert config.tasks.reason.timeout == 20
    assert stamp == loop._file_stamp()
