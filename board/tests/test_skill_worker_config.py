"""Configuration and session-path regressions; no real worker or model execution."""
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from board.dispatcher.config import DispatchConfig, WorkerConfig
from board.dispatcher.skills import prepare_pi_worker
from board.dispatcher.workers.adapters.pi import PiDriver
from conftest import make_config
from test_skill_loading import local_config, make_library, settings_for


def worker_payload(kind="pi", **updates):
    return {"name": "fixture-worker", "type": kind, "task_types": ["explore"],
            "max_running": 1, "priority": 0, **updates}


@pytest.mark.parametrize("kind", ["codex", "claudecode", "mock"])
def test_non_pi_workers_reject_skill_defaults_instead_of_ignoring_them(kind):
    with pytest.raises(ValidationError, match="skill_profiles currently require type pi"):
        WorkerConfig.model_validate(worker_payload(kind, skill_profiles=["source-review"]))
    # Default Pi-only fields must not break otherwise valid non-Pi roundtrips.
    worker = WorkerConfig.model_validate(worker_payload(kind))
    assert WorkerConfig.model_validate(worker.model_dump()) == worker


@pytest.mark.parametrize("settings", [{"pi_extensions": ["builtin:codemode"]}, {"pi_tools": ["read"]}, {"pi_tools": []}])
def test_non_pi_workers_reject_explicit_pi_resource_customization(settings):
    with pytest.raises(ValidationError, match="require type pi"):
        WorkerConfig.model_validate(worker_payload("codex", **settings))


def test_disabled_worker_profile_defaults_fail_during_configuration(tmp_path):
    catalog, _ = make_library(tmp_path)
    with pytest.raises(ValidationError, match="skills.enabled is false"):
        local_config(tmp_path, settings_for(catalog, enabled=False), ["source-review"])


def test_unknown_worker_profile_defaults_fail_during_configuration(tmp_path):
    catalog, _ = make_library(tmp_path)
    with pytest.raises(ValidationError, match="Unknown skill profile"):
        local_config(tmp_path, settings_for(catalog), ["missing-profile"])


def test_nonlocal_worker_profile_defaults_fail_during_configuration():
    payload = make_config().model_dump()
    payload["workers"] = [worker_payload(skill_profiles=["source-review"], env={
        "PI_MODEL": "fixture", "PI_BASE_URL": "http://unused", "PI_API_KEY": "fixture", "PI_PROVIDER_API": "openai-completions",
    })]
    payload["skills"]["enabled"] = True
    with pytest.raises(ValidationError, match="require local execution"):
        DispatchConfig.model_validate(payload)


def test_valid_worker_defaults_are_resolved_and_intent_requests_can_override_them(tmp_path):
    catalog, _ = make_library(tmp_path, entries=[("alpha", ["task:source-review"]), ("beta", ["task:data-inspection"])])
    config, worker, backend = local_config(tmp_path, settings_for(catalog), ["source-review"])
    default_run = prepare_pi_worker(config, worker, backend, "project-one", "default-intent")
    override_run = prepare_pi_worker(config, worker, backend, "project-one", "override-intent", ["general"])
    assert Path(json.loads(default_run.env["BOARD_PI_SKILLS_JSON"])[0]).name == "alpha"
    assert Path(json.loads(override_run.env["BOARD_PI_SKILLS_JSON"])[0]).name == "beta"
    assert worker.skill_profiles == ["source-review"]


def test_runtime_guard_rejects_invalid_copied_non_pi_worker_profile_defaults(tmp_path):
    catalog, _ = make_library(tmp_path)
    config, worker, backend = local_config(tmp_path, settings_for(catalog))
    copied = worker.model_copy(update={"type": "codex", "skill_profiles": ["source-review"]})
    with pytest.raises(ValueError, match="Pi execution worker"):
        prepare_pi_worker(config, copied, backend, "project-one", "invalid-intent")
    assert not list(backend.evidence_dir("project-one", "invalid-intent").iterdir())


def test_project_sessions_are_stable_across_fresh_attempts_and_distinct_across_projects(tmp_path):
    catalog, _ = make_library(tmp_path)
    config, worker, backend = local_config(tmp_path, settings_for(catalog))
    first = prepare_pi_worker(config, worker, backend, "project-one", "first")
    second = prepare_pi_worker(config, worker, backend, "project-one", "second")
    third = prepare_pi_worker(config, worker, backend, "project-two", "third")
    assert first.env["BOARD_PI_SESSION_DIR"] == second.env["BOARD_PI_SESSION_DIR"]
    assert first.env["BOARD_PI_SESSION_DIR"] != third.env["BOARD_PI_SESSION_DIR"]
    driver = PiDriver(local=True)
    assert "--session" not in driver.build_execute(first, "first local fixture", None).argv
    assert "--session" not in driver.build_execute(second, "second local fixture", None).argv
    resumed = driver.build_conclude(first, "conclude", "fixture-session")
    assert resumed[4] == first.env["BOARD_PI_SESSION_DIR"]
    assert resumed[resumed.index("--session")+1] == "fixture-session"


def test_explicit_ephemeral_storage_keeps_the_legacy_worker_session_directory(tmp_path):
    catalog, _ = make_library(tmp_path)
    config, worker, backend = local_config(tmp_path, settings_for(catalog))
    config.local.pi_session_storage = "ephemeral"
    prepared = prepare_pi_worker(config, worker, backend, "project-one", "first")
    assert prepared.env["BOARD_PI_SESSION_DIR"].replace("\\", "/") == "/tmp/board-pi/fixture-pi/sessions"


@pytest.mark.parametrize("storage", ["ephemeral", "project"])
def test_legacy_agent_directory_override_is_preserved_only_for_ephemeral_storage(tmp_path, storage):
    catalog, _ = make_library(tmp_path)
    config, worker, backend = local_config(tmp_path, settings_for(catalog))
    worker.env["PI_AGENT_DIR"] = "/var/lib/existing-pi-sessions"
    config.local.pi_session_storage = storage
    prepared = prepare_pi_worker(config, worker, backend, "project-one", "first")
    expected = ("/var/lib/existing-pi-sessions/fixture-pi/sessions" if storage == "ephemeral"
                else str(backend.project_dir("project-one") / "sessions" / "pi"))
    assert prepared.env["BOARD_PI_SESSION_DIR"] == expected
    assert PiDriver(local=True).build_execute(prepared, "Inspect local fixture", None).argv[4] == expected
    assert worker.env["PI_AGENT_DIR"] == "/var/lib/existing-pi-sessions"
    assert "BOARD_PI_SESSION_DIR" not in worker.env
