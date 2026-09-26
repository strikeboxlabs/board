from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from board.dispatcher.contracts import validate_reason_payload
from board.dispatcher.scheduler.reason_gate import ReasonGate, graph_signature
from board.dispatcher.tasks import reason
from board.dispatcher.runtime.process import ProcessResult
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.workers.adapters.pi import PiDriver
from board.reporting.efficiency import summarize
from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_project, make_intent


def stream(messages):
    return '\n'.join([json.dumps({'type': 'session', 'id': 'metadata'}), *[
        json.dumps({'type': 'turn_end', 'message': m}) for m in messages],
        json.dumps({'type': 'agent_end', 'messages': messages})])


def message(stop='stop', text='{"accepted":true,"data":{}}'):
    return {'role': 'assistant', 'stopReason': stop,
            'content': [{'type': 'text', 'text': text}] if text else [{'type': 'thinking', 'thinking': 'private'}],
            'usage': {'input': 10, 'output': 20, 'reasoning': 15, 'cacheRead': 30, 'totalTokens': 60}}


@pytest.mark.parametrize(('stop', 'text', 'error'), [('length', '', 'pi_output_limit'), ('length', '{"accepted":', 'pi_output_limit'), ('stop', '', 'pi_empty_final_answer')])
def test_pi_does_not_parse_metadata_or_truncated_answers(stop, text, error):
    with pytest.raises(ValueError, match=error):
        PiDriver().extract_response_text(stream([message(stop, text)]), '')


def test_usage_counts_model_calls_once_and_extracts_final_text():
    output = stream([message('toolUse', 'reading'), message()])
    assert PiDriver().extract_response_text(output, '') == '{"accepted":true,"data":{}}'
    d = PiDriver.output_diagnostics(output)
    assert d['model_calls'] == 2
    assert d['usage']['output'] == 40
    assert d['usage']['reasoning'] == 30
    assert d['usage']['totalTokens'] == 120


def test_retry_limit_backoff_and_restart(tmp_path):
    backend = FakeContainerManager()
    config = make_config().tasks.reason
    gate = ReasonGate(backend, config)
    project = make_project()
    sig = graph_signature(project, config)
    with patch('board.dispatcher.scheduler.reason_gate.time.time', return_value=100):
        assert gate.allows('proj_001', sig)
        gate.started('proj_001', sig)
        gate.finished('proj_001', 'failed')
        assert not gate.allows('proj_001', sig)
    for now in (200, 400):
        with patch('board.dispatcher.scheduler.reason_gate.time.time', return_value=now):
            gate = ReasonGate(backend, config)
            assert gate.allows('proj_001', sig)
            gate.started('proj_001', sig)
            gate.finished('proj_001', 'failed')
    with patch('board.dispatcher.scheduler.reason_gate.time.time', return_value=1000):
        gate = ReasonGate(backend, config)
        assert not gate.allows('proj_001', sig)
        project.facts[0].description = 'new evidence'
        assert gate.allows('proj_001', graph_signature(project, config))


def test_successful_wait_does_not_spin_after_restart():
    backend = FakeContainerManager()
    config = make_config().tasks.reason
    gate = ReasonGate(backend, config)
    sig = graph_signature(make_project(), config)
    gate.started('proj_001', sig)
    gate.finished('proj_001', 'success')
    with patch('board.dispatcher.scheduler.reason_gate.time.time', return_value=10**12):
        assert not ReasonGate(backend, config).allows('proj_001', sig)
    assert validate_reason_payload({'accepted': True, 'data': {'wait': 'Need user information'}}, True, 3)[0] == 'wait'


def run_planner(monkeypatch, project, output, client=None):
    config = make_config()
    backend = FakeContainerManager()
    client = client or FakeClient(project)
    driver = FakeDriver()
    monkeypatch.setattr(reason, 'get_driver', lambda *_: driver)
    monkeypatch.setattr(reason.HeartbeatLease, 'for_reason', lambda *_: FakeLease())
    monkeypatch.setattr(reason, 'run_worker_process', lambda *a, **kw: ProcessResult(0, json.dumps(output), ''))
    result = reason.run_reason_task(config, client, backend, project, 'graph', config.workers[0], TaskCancellation())
    return result, client, driver


def test_only_fills_vacancy_and_does_not_require_three(monkeypatch):
    project = make_project(intents=[make_intent('i001'), make_intent('i002')])
    output = {'accepted': True, 'data': {'intents': [{'from': ['f001'], 'description': f'next {i}'} for i in range(3)]}}
    result, client, driver = run_planner(monkeypatch, project, output)
    assert result == 'success'
    assert len(client.created_intents) == 1
    assert 'at most 1' in driver.execute_prompts[0]


def test_capacity_rechecked_after_human_adds_intent(monkeypatch):
    project = make_project(intents=[make_intent('i001'), make_intent('i002')])
    client = FakeClient(make_project(intents=[make_intent(f'i00{i}') for i in range(1, 4)]))
    result, client, _ = run_planner(monkeypatch, project, {'accepted': True, 'data': {'intents': [{'from': ['f001'], 'description': 'next'}]}}, client)
    assert result == 'success'
    assert not client.created_intents


def test_pi_reason_is_tool_free_file_attachment(monkeypatch):
    config = make_config()
    config.runtime.execution = 'local'
    config.workers[0].type = 'pi'
    backend = FakeContainerManager()
    driver = PiDriver(local=True)
    client = FakeClient(make_project())
    monkeypatch.setattr(reason, 'get_driver', lambda *_: driver)
    monkeypatch.setattr(reason.HeartbeatLease, 'for_reason', lambda *_: FakeLease())
    commands = []
    def run(*args, **kwargs):
        commands.append(args[3])
        return ProcessResult(0, stream([message(text='{"accepted":true,"data":{"wait":"No independent work yet"}}')]), '')
    monkeypatch.setattr(reason, 'run_worker_process', run)
    result = reason.run_reason_task(config, client, backend, client.project, 'the whole graph', config.workers[0], TaskCancellation())
    assert result == 'success'
    assert '--no-tools' in commands[0]
    assert '--tools' not in commands[0]
    assert commands[0][-1].startswith('@')
    prompt = next(content for _, path, content in backend.writes if path.endswith('.md'))
    extension = next(content for _, path, content in backend.writes if path.endswith('.mjs'))
    assert 'the whole graph' in prompt
    assert 'p.reasoning_effort = "low"' in extension
    assert 'p[key] = 16384' in extension


def test_efficiency_summary_does_not_double_count_reasoning(tmp_path):
    path = tmp_path / 'efficiency.jsonl'
    event = {'timestamp': '2026-09-26T10:00:00+00:00', 'event': 'process', 'phase': 'reason_execute', 'elapsed_seconds': 10, **PiDriver.output_diagnostics(stream([message()]))}
    path.write_text(json.dumps(event) + '\n' + '{partial')
    result = summarize(path)
    assert result['tokens']['totalTokens'] == 60
    assert result['malformed_lines'] == 1


def test_startup_vacancy_triggers_planning_and_restores_checkpoint():
    from board.dispatcher.scheduler.loop import DispatcherLoop
    from test_scheduler_logic import _summary
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = make_config()
    loop.reason_checkpoints = {}
    loop.reason_gate = ReasonGate(FakeContainerManager(), loop.config.tasks.reason)
    summary = _summary('proj_001', 'active')
    summary.working_intent_count = 1
    loop._initialize_reason_checkpoints([summary])
    assert 'proj_001' not in loop.reason_checkpoints
    project = make_project(intents=[make_intent()])
    assert loop._reason_trigger(project) == 'initial'
    loop.reason_gate.started('proj_001', graph_signature(project, loop.config.tasks.reason))
    loop.reason_gate.finished('proj_001', 'success', {'fact_count': 2, 'hint_count': 1, 'open_intent_count': 2})
    loop._initialize_reason_checkpoints([summary])
    # A fact arriving during the earlier call must survive restart as pending work.
    assert loop._reason_trigger(project) == 'facts:2->3'
