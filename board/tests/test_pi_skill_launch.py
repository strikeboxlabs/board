"""Local Pi argv and task-resume integration; no real model or scanner."""
import json
from pathlib import Path

import pytest
import yaml

from board.dispatcher.config import WorkerConfig
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.runtime.process import ProcessResult
from board.dispatcher.tasks import explore
from board.dispatcher.tasks.common import focus_graph_yaml
from board.dispatcher.workers.adapters.pi import PiDriver
from board.dispatcher.skills import prepare_pi_worker
from conftest import FakeClient, FakeLease, make_intent, make_project
from test_skill_loading import local_config, make_library, settings_for


def test_local_arguments_preserve_host_auth_and_frozen_resources(tmp_path):
    selected = [str(tmp_path / "one"), str(tmp_path / "two")]
    guide = str(tmp_path / "board-guide.md")
    session_dir = str(tmp_path / "project" / "sessions" / "pi")
    worker = WorkerConfig(name="pi", type="pi", task_types=["explore"], max_running=1, priority=0,
        env={"BOARD_PI_SKILLS_JSON": json.dumps(selected), "BOARD_PI_CONTEXT_FILE": guide,
             "BOARD_PI_SESSION_DIR": session_dir},
        pi_extensions=["builtin:codemode"], pi_tools=["read", "bash", "codemode"])
    driver = PiDriver(local=True)
    execute = driver.build_execute(worker, "first", None).argv
    conclude = driver.build_conclude(worker, "conclude", "fixture-session")
    assert execute[:2] == ["/bin/sh", "-lc"]
    assert execute[4] == conclude[4] == session_dir
    assert execute[2].count("--session-dir") == 1
    assert "umask 077" in execute[2]
    for argv in (execute, conclude):
        assert "--provider" not in argv and "--model" not in argv
        assert "PI_CODING_AGENT_DIR" not in argv[2]
        assert [argv[i+1] for i, arg in enumerate(argv[:-1]) if arg == "--skill"] == selected
        assert argv[argv.index("--append-system-prompt")+1] == guide
        assert argv[argv.index("--tools")+1] == "read,bash,codemode"
        assert argv[argv.index("--extension")+1] == "builtin:codemode"
        assert "--no-skills" in argv and "--no-context-files" in argv and "--no-approve" in argv
    assert conclude[-4:] == ["--session", "fixture-session", "-p", "conclude"]


@pytest.mark.parametrize("value", ["null", "42", '[1]', '[""]', '{"path":"x"}'])
def test_invalid_resolved_skill_paths_fail_before_launch(value):
    worker = WorkerConfig(name="pi", type="pi", task_types=["explore"], max_running=1,
        priority=0, env={"BOARD_PI_SKILLS_JSON": value})
    with pytest.raises(ValueError):
        PiDriver(local=True).build_execute(worker, "fixture", None)


def test_jsonl_preserves_unicode_line_separators_inside_messages():
    text = "owned fixture\u2028second line\u2029third line"
    stdout = json.dumps({"type": "agent_end", "messages": [{"role": "assistant",
        "stopReason": "stop", "content": [{"type": "text", "text": text}]}]}, ensure_ascii=False) + "\n"
    assert PiDriver(local=True).extract_response_text(stdout, "") == text


def test_focused_graph_keeps_dependency_provenance_and_full_project_constraints():
    graph = {"project": {"title": "fixture", "execution_notes": "local reads only"},
        "hints": [{"content": "retain scope"}],
        "facts": [{"id": key, "description": key} for key in ["origin", "goal", "parent", "source", "unrelated"]],
        "intents": [{"from": ["origin"], "to": "parent"}, {"from": ["parent"], "to": "source"},
                    {"from": ["origin"], "to": "unrelated"}],
        "jobs": [{"fact": "source", "id": "related"}, {"fact": "unrelated", "id": "other"}]}
    focused = yaml.safe_load(focus_graph_yaml(yaml.safe_dump(graph), ["source"]))
    assert {fact["id"] for fact in focused["facts"]} == {"origin", "goal", "parent", "source"}
    assert len(focused["intents"]) == 2
    assert focused["jobs"] == [{"fact": "source", "id": "related"}]
    assert focused["project"] == graph["project"] and focused["hints"] == graph["hints"]
    assert focused["context_view"]["omitted_facts"] == 1


def test_real_task_fallback_reuses_the_frozen_selection_and_persistent_session(tmp_path, monkeypatch):
    catalog, _ = make_library(tmp_path)
    config, worker, backend = local_config(tmp_path, settings_for(catalog))
    intent = make_intent()
    intent.skill_profiles = ["source-review"]
    project = make_project(intents=[intent])
    client = FakeClient(project)
    captured = []
    final = {"accepted": True, "data": {"description": "checked supplied fixture"}}
    outputs = iter([
        ProcessResult(0, json.dumps({"type": "session", "id": "fixture-session"})+"\n", ""),
        ProcessResult(0, json.dumps({"type": "agent_end", "messages": [{"role": "assistant",
            "stopReason": "stop", "content": [{"type": "text", "text": json.dumps(final)}]}]})+"\n", "")])
    def run(*args, **kwargs):
        captured.append((args[2], args[3]))
        return next(outputs)
    monkeypatch.setattr(explore, "get_driver", lambda *args: PiDriver(local=True))
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", lambda *args: FakeLease())
    monkeypatch.setattr(explore, "_run_process", run)
    outcome = explore.run_explore_task(config, client, backend, project,
        "facts:\n- id: f001\n  description: supplied fixture\n", intent, worker, TaskCancellation())
    assert outcome == "success"
    assert len(captured) == 2
    first_worker, first_argv = captured[0]
    second_worker, second_argv = captured[1]
    assert first_worker is second_worker and first_worker is not worker
    assert first_worker.env["BOARD_PI_SKILLS_JSON"] == second_worker.env["BOARD_PI_SKILLS_JSON"]
    assert first_worker.env["BOARD_PI_CONTEXT_FILE"] == second_worker.env["BOARD_PI_CONTEXT_FILE"]
    assert first_argv[4] == second_argv[4] == str(backend.project_dir(project.project.id) / "sessions" / "pi")
    assert second_argv[second_argv.index("--session")+1] == "fixture-session"
    assert worker.env == {"FIXTURE_SETTING": "preserved"}
    manifest = json.loads(Path(first_worker.env["BOARD_PI_SELECTION_FILE"]).read_text(encoding="utf-8"))
    assert manifest["profiles"] == ["source-review"] and manifest["skill_count"] == 1
    assert client.concluded[-1][-1] == "checked supplied fixture"
