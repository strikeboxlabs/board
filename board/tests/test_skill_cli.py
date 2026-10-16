"""Native skill diagnostics stay local and keep invalid configuration secrets out of output."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess

from click.testing import CliRunner
import pytest
import requests
import yaml

from board.cli import main

SECRET = "CREDENTIAL_FIXTURE_NEVER_ECHO_8293"


@pytest.fixture(autouse=True)
def no_runtime_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Skill diagnostics must not contact providers or launch worker processes")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def config_document(tmp_path: Path) -> dict:
    return {
        "server": "http://127.0.0.1:8765",
        "runtime": {"execution": "local", "worker_healthcheck": "disabled",
                    "max_workers": 2, "max_running_projects": 1,
                    "max_project_workers": 2, "interval": 3,
                    "healthcheck_timeout": 15, "prompt_group": "default"},
        "tasks": {"bootstrap": {"timeout": 120, "conclude_timeout": 30},
                  "reason": {"timeout": 60},
                  "explore": {"timeout": 300, "conclude_timeout": 60}},
        "local": {"evidence_root": str(tmp_path / "unused-runs")},
        "workers": [{"name": "fixture-pi", "type": "pi", "priority": 0,
                     "task_types": ["bootstrap", "reason", "explore"], "max_running": 1}],
        "common_env": {},
        "skills": {"enabled": False, "catalogs": [], "default_profiles": ["general"]},
    }


def write_config(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "dispatch.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def invoke(command: str, config: Path):
    return CliRunner().invoke(main, ["skills", command, "--config", str(config), "--json"])


def assert_safe_failure(result):
    assert result.exit_code != 0
    diagnostic = result.output + str(result.exception)
    assert SECRET not in diagnostic
    assert "input_value" not in diagnostic
    assert "Traceback" not in diagnostic
    # Unhandled parser/Pydantic exceptions would cause a traceback in a real console.
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("command", ["doctor", "profiles"])
@pytest.mark.parametrize("location", ["common_env", "worker_env"])
def test_invalid_config_diagnostics_redact_credential_values(tmp_path, command, location):
    document = config_document(tmp_path)
    invalid_secret_value = {"PRIVATE_TOKEN": {"value": SECRET}}
    if location == "common_env":
        document["common_env"] = invalid_secret_value
    else:
        document["workers"][0]["env"] = invalid_secret_value
    result = invoke(command, write_config(tmp_path, document))
    assert_safe_failure(result)
    if command == "doctor":
        assert json.loads(result.output)["errors"]


@pytest.mark.parametrize("command", ["doctor", "profiles"])
def test_malformed_yaml_diagnostics_do_not_quote_secret_source_lines(tmp_path, command):
    config = tmp_path / "dispatch.yaml"
    config.write_text('common_env:\n  PRIVATE_TOKEN: ["' + SECRET + '"\n', encoding="utf-8")
    result = invoke(command, config)
    assert_safe_failure(result)
    if command == "doctor":
        assert json.loads(result.output)["errors"]


def test_doctor_validates_disabled_skills_without_model_or_worker_calls(tmp_path):
    result = invoke("doctor", write_config(tmp_path, config_document(tmp_path)))
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["enabled"] is False
    assert report["errors"] == []
    assert report["profiles"] == []
    assert not (tmp_path / "unused-runs").exists()


def test_doctor_rejects_catalog_json_with_an_invalid_root(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text("[]", encoding="utf-8")
    document = config_document(tmp_path)
    document["skills"].update(enabled=True, catalogs=[str(catalog)])
    result = invoke("doctor", write_config(tmp_path, document))
    assert result.exit_code != 0
    assert json.loads(result.output)["errors"]
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_profiles_lists_an_explicit_trusted_profile_without_model_calls(tmp_path):
    package = tmp_path / "library/skills/fixture-reader"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: fixture-reader\ndescription: Read supplied fixture text.\n---\nLocal fixture.\n",
        encoding="utf-8")
    catalog = tmp_path / "library/catalog.json"
    catalog.write_text(json.dumps({"schema_version": 1, "library_id": "cli-fixture",
        "skills": [{"id": "fixture-reader", "path": "skills/fixture-reader",
                    "tags": ["task:data-inspection"], "requires": []}]}), encoding="utf-8")
    document = config_document(tmp_path)
    document["skills"].update(enabled=True, catalogs=[str(catalog)],
        default_profiles=["review-only"],
        profiles={"review-only": {"description": "Read only supplied fixture packages.",
                                  "skills": ["cli-fixture/fixture-reader"]}})
    result = invoke("profiles", write_config(tmp_path, document))
    assert result.exit_code == 0, result.output
    profiles = {profile["id"]: profile for profile in json.loads(result.output)}
    assert profiles["review-only"]["skills"] == 1
    assert profiles["review-only"]["estimated_metadata_tokens"] > 0
    assert not (tmp_path / "unused-runs").exists()
