"""Planner profile validation against owned local catalogs and mocked processes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from board.dispatcher import skills
from board.dispatcher.contracts import UnavailableSkillProfileError, validate_reason_payload
from board.dispatcher.runtime.cancellation import TaskCancellation
from board.dispatcher.runtime.process import ProcessResult
from board.dispatcher.tasks import reason
from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_project
from test_skill_loading import make_library, settings_for


def configured_profiles(tmp_path):
    catalog, _ = make_library(tmp_path)
    return settings_for(catalog, profiles={
        "custom-review": {"description": "Review the local fixture.", "extends": ["source-review"]},
        "empty-profile": {"description": "No matching supplied fixture.", "tags": ["language:java"]},
    })


def plan(*profiles):
    return {"accepted": True, "data": {"intents": [
        {"from": ["f001"], "description": f"Inspect local fixture {index}", "skill_profiles": ids}
        for index, ids in enumerate(profiles)
    ]}}


def test_advertised_snapshot_contains_only_resolvable_nonempty_profiles(tmp_path, monkeypatch):
    settings = configured_profiles(tmp_path)
    available = skills.available_skill_profiles(settings)
    assert set(available) == {"source-review", "custom-review"}
    assert available["custom-review"].skill_count == 1
    # Rendering this snapshot must not resolve the catalog a second time.
    monkeypatch.setattr(skills, "load_catalogs", lambda *_: pytest.fail("Catalog reloaded while rendering snapshot"))
    text = skills.profile_summaries(settings, available)
    assert "- source-review:" in text and "- custom-review:" in text
    assert "empty-profile" not in text and "- java:" not in text


@pytest.mark.parametrize("max_intents", [0, 1, 3])
def test_contract_rejects_invalid_later_element_before_capacity_truncation(max_intents):
    with pytest.raises(UnavailableSkillProfileError) as failure:
        validate_reason_payload(plan(["custom-review"], ["unknown-profile"]), False, max_intents,
                                allowed_skill_profiles={"source-review", "custom-review"})
    assert failure.value.intent_index == 1
    assert failure.value.unavailable_ids == ["unknown-profile"]
    assert "Valid profile IDs: custom-review, source-review." in str(failure.value)


@pytest.mark.parametrize("ids", [[], ["custom-review"]])
def test_contract_preserves_empty_and_custom_requests(ids):
    kind, intents = validate_reason_payload(plan(ids), True, 1, allowed_skill_profiles={"custom-review"})
    assert kind == "intents" and intents[0]["skill_profiles"] == ids


def test_disabled_profiles_allow_omission_and_empty_but_reject_explicit_request(monkeypatch):
    settings = skills.SkillsConfig(enabled=False)
    monkeypatch.setattr(skills, "load_catalogs", lambda *_: pytest.fail("Disabled profiles loaded a catalog"))
    assert skills.available_skill_profiles(settings) == {}
    without_profiles = plan([])
    del without_profiles["data"]["intents"][0]["skill_profiles"]
    assert validate_reason_payload(without_profiles, True, 1, allowed_skill_profiles=set())[0] == "intents"
    assert validate_reason_payload(plan([]), True, 1, allowed_skill_profiles=set())[0] == "intents"
    with pytest.raises(UnavailableSkillProfileError, match=r"none \(omit skill_profiles\)"):
        validate_reason_payload(plan(["source-review"]), True, 1, allowed_skill_profiles=set())


def run_mocked_planner(monkeypatch, config, payload, during_process=None):
    project = make_project()
    client = FakeClient(project)
    driver = FakeDriver()
    monkeypatch.setattr(reason, "get_driver", lambda *_: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *_: FakeLease())

    def process(*_args, **_kwargs):
        if during_process:
            during_process()
        return ProcessResult(0, json.dumps(payload), "")

    monkeypatch.setattr(reason, "run_worker_process", process)
    outcome = reason.run_reason_task(config, client, FakeContainerManager(), project, "facts: []",
                                     config.workers[0], TaskCancellation())
    return outcome, client, driver


@pytest.mark.parametrize("invalid", ["web", "empty-profile", "java"])
def test_planner_invalid_later_profile_writes_no_intents(tmp_path, monkeypatch, caplog, invalid):
    config = make_config()
    config.skills = configured_profiles(tmp_path)
    config.tasks.reason.max_intents = 1
    payload = plan(["custom-review"], [invalid])
    payload["private_debug"] = "RAW_MODEL_OUTPUT_MUST_NOT_APPEAR"
    outcome, client, _ = run_mocked_planner(monkeypatch, config, payload)
    assert outcome == "failed"
    assert client.created_intents == [] and client.requested_skill_profiles == []
    assert f"unavailable skill profile IDs: {invalid}" in caplog.text
    assert "Valid profile IDs: custom-review, source-review." in caplog.text
    assert "RAW_MODEL_OUTPUT_MUST_NOT_APPEAR" not in caplog.text


def test_planner_uses_same_snapshot_for_prompt_and_response_validation(tmp_path, monkeypatch):
    config = make_config()
    config.skills = configured_profiles(tmp_path)
    original = reason.available_skill_profiles
    snapshots = []

    def snapshot(settings):
        result = original(settings)
        snapshots.append(result)
        return result

    monkeypatch.setattr(reason, "available_skill_profiles", snapshot)

    def change_fixture_catalog():
        # A filesystem change during the model call must not silently change
        # which choices that same call advertised and accepts.
        catalog = Path(config.skills.catalogs[0])
        data = json.loads(catalog.read_text())
        data["skills"] = []
        catalog.write_text(json.dumps(data))

    outcome, client, driver = run_mocked_planner(monkeypatch, config, plan(["custom-review"]), change_fixture_catalog)
    assert outcome == "success" and len(snapshots) == 1
    assert client.requested_skill_profiles == [["custom-review"]]
    assert "- custom-review: Review the local fixture. (1 skills)" in driver.execute_prompts[0]
    assert "empty-profile" not in driver.execute_prompts[0]


def test_disabled_planner_rejects_explicit_profiles_without_writes(monkeypatch):
    outcome, client, driver = run_mocked_planner(monkeypatch, make_config(), plan(["source-review"]))
    assert outcome == "failed" and client.created_intents == []
    assert "Skill profiles are disabled" in driver.execute_prompts[0]
