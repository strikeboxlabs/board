"""Trusted catalogue and per-attempt resource tests; no agent/tool execution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from board.dispatcher.config import DispatchConfig, LocalConfig, WorkerConfig
from board.dispatcher.runtime.local_backend import LocalBackend
from board.dispatcher import skills as engine
from board.dispatcher.skills import SkillsConfig

from conftest import make_config


def make_library(tmp_path: Path, library="fixture", entries=None):
    root = tmp_path / library
    root.mkdir(parents=True)
    entries = entries or [("alpha", ["task:source-review"])]
    records = []
    for name, tags in entries:
        directory = root / "skills" / name
        (directory / "references").mkdir(parents=True)
        (directory / "assets").mkdir()
        (directory / "references" / "context.md").write_text("Original reference.\n", encoding="utf-8")
        (directory / "assets" / "fixture.txt").write_bytes(b"fixture\x00bytes\n")
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Inspect supplied harmless fixture {name}.\n---\n\n"
            "Read the complete entrypoint and the applicable [reference](references/context.md).\n",
            encoding="utf-8")
        records.append({"id": name, "path": f"skills/{name}", "tags": tags, "requires": []})
    catalog = root / "catalog.json"
    data = {"schema_version": 1, "library_id": library, "skills": records}
    catalog.write_text(json.dumps(data), encoding="utf-8")
    return catalog, data


def rewrite(catalog, data):
    catalog.write_text(json.dumps(data), encoding="utf-8")


def settings_for(catalog, **changes):
    return SkillsConfig.model_validate({"enabled": True, "catalogs": [str(catalog)],
        "default_profiles": ["source-review"], **changes})


def local_config(tmp_path, settings, worker_profiles=None):
    worker = WorkerConfig(name="fixture-pi", type="pi", task_types=["explore"],
        max_running=1, priority=0, env={"FIXTURE_SETTING": "preserved"},
        skill_profiles=worker_profiles or [])
    payload = make_config().model_dump()
    payload["runtime"]["execution"] = "local"
    payload["container"] = None
    payload["local"] = {"evidence_root": str(tmp_path / "projects")}
    payload["workers"] = [worker.model_dump()]
    payload["skills"] = settings.model_dump()
    config = DispatchConfig.model_validate(payload)
    backend = LocalBackend(config.local)
    backend.ensure_running("project-one")
    return config, config.workers[0], backend


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(schema_version=2),
    lambda data: data.update(schema_version=True),
    lambda data: data.update(unrecognized="field"),
    lambda data: data.update(library_id="../outside"),
    lambda data: data.update(skills="not-an-array"),
    lambda data: data["skills"][0].update(extra="not-an-entry-field"),
    lambda data: data["skills"][0].update(tags="not-an-array"),
    lambda data: data["skills"][0].update(tags=["task:source-review", "task:source-review"]),
    lambda data: data["skills"][0].update(tags=["unrecognized:facet"]),
    lambda data: data["skills"][0].update(requires=["", "unavailable"]),
])
def test_rejects_invalid_catalog_contract(tmp_path, mutation):
    catalog, data = make_library(tmp_path)
    mutation(data)
    rewrite(catalog, data)
    with pytest.raises(ValueError):
        engine.load_catalogs(settings_for(catalog))


@pytest.mark.parametrize("relative", ["../outside", "/outside", "C:/outside", "skills\\alpha", "skills/./alpha", "skills/alpha/"])
def test_rejects_unsafe_or_noncanonical_package_paths(tmp_path, relative):
    catalog, data = make_library(tmp_path)
    data["skills"][0]["path"] = relative
    rewrite(catalog, data)
    with pytest.raises((ValueError, OSError)):
        engine.load_catalogs(settings_for(catalog))


def test_catalog_id_requires_matching_directory_shape(tmp_path):
    catalog, data = make_library(tmp_path)
    other = catalog.parent / "other"
    other.mkdir()
    (other / "SKILL.md").write_text("---\nname: alpha\ndescription: A harmless fixture.\n---\n", encoding="utf-8")
    data["skills"][0]["path"] = "other"
    rewrite(catalog, data)
    with pytest.raises(ValueError):
        engine.load_catalogs(settings_for(catalog))


def test_duplicate_json_keys_are_rejected(tmp_path):
    catalog, _ = make_library(tmp_path)
    catalog.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        engine.load_catalogs(settings_for(catalog))


def test_duplicate_package_and_cross_library_pi_names_are_rejected(tmp_path):
    catalog, data = make_library(tmp_path)
    data["skills"].append(dict(data["skills"][0]))
    rewrite(catalog, data)
    with pytest.raises(ValueError, match="collision"):
        engine.load_catalogs(settings_for(catalog))
    catalog, _ = make_library(tmp_path / "replacement")
    other, _ = make_library(tmp_path, library="second")
    with pytest.raises(ValueError, match="collision"):
        engine.load_catalogs(settings_for(catalog, catalogs=[str(catalog), str(other)]))


def test_duplicate_library_identity_is_rejected(tmp_path):
    first, _ = make_library(tmp_path / "one", entries=[("alpha", [])])
    second, _ = make_library(tmp_path / "two", entries=[("beta", [])])
    with pytest.raises(ValueError, match="Duplicate skill library"):
        engine.load_catalogs(settings_for(first, catalogs=[str(first), str(second)]))


@pytest.mark.parametrize("frontmatter", [
    "---\nname: different\ndescription: Valid description.\n---\n",
    "---\nname: alpha\ndescription: ''\n---\n",
    "No frontmatter at all.\n",
])
def test_requires_matching_name_and_usable_discovery_text(tmp_path, frontmatter):
    catalog, _ = make_library(tmp_path)
    (catalog.parent / "skills" / "alpha" / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    with pytest.raises(ValueError):
        engine.load_catalogs(settings_for(catalog))


def test_resource_symlinks_are_rejected_before_copy(tmp_path):
    catalog, _ = make_library(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("Must not be included.\n", encoding="utf-8")
    link = catalog.parent / "skills" / "alpha" / "references" / "linked.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Host cannot create fixture symlinks: {error}")
    with pytest.raises(ValueError, match="symlink"):
        engine.load_catalogs(settings_for(catalog))


def test_profile_inheritance_combines_explicit_ids_and_or_tags_without_duplicates(tmp_path):
    catalog, _ = make_library(tmp_path, entries=[
        ("alpha", ["task:source-review", "language:python"]),
        ("beta", ["task:data-inspection"]),
        ("gamma", ["classification:unreviewed", "execution:linux"]),
    ])
    settings = settings_for(catalog, profiles={
        "base": {"skills": ["fixture/alpha"]},
        "child": {"extends": ["base"], "tags": ["language:python", "task:data-inspection"]},
    })
    expanded, selected = engine.resolve_skills(settings, engine.load_catalogs(settings), ["child", "base"])
    assert expanded == ("base", "child")
    assert [skill.id for skill in selected] == ["fixture/alpha", "fixture/beta"]


def test_unreviewed_classification_is_excluded_from_tag_selection(tmp_path):
    catalog, _ = make_library(tmp_path, entries=[
        ("alpha", ["task:source-review"]),
        ("beta", ["classification:unreviewed", "execution:linux"]),
    ])
    settings = settings_for(catalog, profiles={"linux-view": {"tags": ["execution:linux"]}})
    _, selected = engine.resolve_skills(settings, engine.load_catalogs(settings), ["linux-view"])
    assert selected == ()
    settings = settings_for(catalog, profiles={"explicit": {"skills": ["fixture/beta"]}})
    _, selected = engine.resolve_skills(settings, engine.load_catalogs(settings), ["explicit"])
    assert [skill.id for skill in selected] == ["fixture/beta"]


@pytest.mark.parametrize("profiles", [
    {"child": {"extends": ["missing"]}},
    {"first": {"extends": ["second"]}, "second": {"extends": ["first"]}},
])
def test_unknown_parents_and_profile_cycles_are_configuration_errors(profiles):
    with pytest.raises(ValidationError):
        SkillsConfig(enabled=True, profiles=profiles)


def test_unknown_requested_profile_and_missing_explicit_skill_are_errors(tmp_path):
    catalog, _ = make_library(tmp_path)
    settings = settings_for(catalog, profiles={"missing-skill": {"skills": ["fixture/absent"]}})
    loaded = engine.load_catalogs(settings)
    with pytest.raises(ValueError, match="Unknown skill profile"):
        engine.resolve_skills(settings, loaded, ["unknown"])
    with pytest.raises(ValueError, match="missing skill"):
        engine.resolve_skills(settings, loaded, ["missing-skill"])


def test_metadata_budget_rejects_the_complete_selection_without_truncating(tmp_path):
    catalog, _ = make_library(tmp_path, entries=[("alpha", ["task:source-review"]), ("beta", ["task:source-review"])])
    settings = settings_for(catalog, max_metadata_tokens=128)
    config, worker, backend = local_config(tmp_path, settings)
    with pytest.raises(ValueError, match="exceeding"):
        engine.prepare_pi_worker(config, worker, backend, "project-one", "budget-attempt")
    assert not list(backend.evidence_dir("project-one", "budget-attempt").glob("skill-selection-*.json"))
    assert len(engine.load_catalogs(settings)) == 2
    config.skills = settings_for(catalog, max_metadata_tokens=6000)
    prepared = engine.prepare_pi_worker(config, worker, backend, "project-one", "complete-attempt")
    assert len(json.loads(prepared.env["BOARD_PI_SKILLS_JSON"])) == 2


def test_snapshot_preserves_resources_and_records_provenance_without_mutating_worker(tmp_path):
    catalog, _ = make_library(tmp_path)
    settings = settings_for(catalog)
    config, worker, backend = local_config(tmp_path, settings)
    original_env = dict(worker.env)
    prepared = engine.prepare_pi_worker(config, worker, backend, "project-one", "intent-one")
    snapshot = Path(json.loads(prepared.env["BOARD_PI_SKILLS_JSON"])[0])
    manifest_path = Path(prepared.env["BOARD_PI_SELECTION_FILE"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["skills"][0]
    assert prepared is not worker
    assert worker.env == original_env
    assert snapshot.is_relative_to(Path(backend.scratch_dir("project-one")))
    assert manifest_path.parent == backend.evidence_dir("project-one", "intent-one")
    assert manifest["project_id"] == "project-one"
    assert manifest["intent_id"] == "intent-one"
    assert manifest["profiles"] == ["source-review"]
    assert entry["catalog_sha256"] == sha256(catalog.read_bytes()).hexdigest()
    assert entry["package_sha256"] == engine.package_digest(snapshot)
    assert (snapshot / "assets" / "fixture.txt").read_bytes() == b"fixture\x00bytes\n"
    context = Path(prepared.env["BOARD_PI_CONTEXT_FILE"])
    assert manifest["context_sha256"] == sha256(context.read_bytes()).hexdigest()
    source_reference = catalog.parent / "skills" / "alpha" / "references" / "context.md"
    source_reference.write_text("Changed source after preparation.\n", encoding="utf-8")
    assert (snapshot / "references" / "context.md").read_text(encoding="utf-8") == "Original reference.\n"
    assert engine.package_digest(snapshot) == entry["package_sha256"]
    assert engine.package_digest(source_reference.parents[1]) != entry["package_sha256"]


def test_source_change_during_snapshot_is_detected(tmp_path, monkeypatch):
    catalog, _ = make_library(tmp_path)
    config, worker, backend = local_config(tmp_path, settings_for(catalog))
    actual_copy = engine.shutil.copytree
    changed = False

    def copy_then_change(source, destination, *args, **kwargs):
        nonlocal changed
        result = actual_copy(source, destination, *args, **kwargs)
        if not changed and Path(source).name == "alpha":
            changed = True
            (Path(source) / "references" / "context.md").write_text("Concurrent edit.\n", encoding="utf-8")
        return result

    monkeypatch.setattr(engine.shutil, "copytree", copy_then_change)
    with pytest.raises(ValueError, match="changed during snapshot"):
        engine.prepare_pi_worker(config, worker, backend, "project-one", "changed-attempt")
    assert not list(backend.evidence_dir("project-one", "changed-attempt").glob("skill-selection-*.json"))


def test_concurrent_attempts_have_independent_selections_and_frozen_directories(tmp_path):
    catalog, _ = make_library(tmp_path, entries=[("alpha", ["task:source-review"]), ("beta", ["task:data-inspection"])])
    settings = settings_for(catalog, profiles={"alpha-only": {"skills": ["fixture/alpha"]}, "beta-only": {"skills": ["fixture/beta"]}})
    config, worker, backend = local_config(tmp_path, settings)

    def prepare(index):
        profile = "alpha-only" if index % 2 == 0 else "beta-only"
        prepared = engine.prepare_pi_worker(config, worker, backend, "project-one", f"intent-{index}", [profile])
        manifest = json.loads(Path(prepared.env["BOARD_PI_SELECTION_FILE"]).read_text(encoding="utf-8"))
        return profile, prepared, manifest

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(prepare, range(8)))
    snapshots, attempts = set(), set()
    for profile, prepared, manifest in results:
        paths = json.loads(prepared.env["BOARD_PI_SKILLS_JSON"])
        assert len(paths) == 1
        assert manifest["profiles"] == [profile]
        assert manifest["skills"][0]["id"] == "fixture/" + profile.removesuffix("-only")
        snapshots.update(paths)
        attempts.add(manifest["attempt_id"])
    assert len(snapshots) == len(attempts) == 8
    assert worker.env == {"FIXTURE_SETTING": "preserved"}


def test_disabled_skills_do_not_load_catalogs_and_reject_explicit_requests(tmp_path, monkeypatch):
    settings = SkillsConfig(enabled=False, catalogs=[str(tmp_path / "missing.json")])
    config, worker, backend = local_config(tmp_path, settings)
    monkeypatch.setattr(engine, "load_catalogs", lambda settings: pytest.fail("Disabled skills must not load catalogues"))
    prepared = engine.prepare_pi_worker(config, worker, backend, "project-one")
    assert json.loads(prepared.env["BOARD_PI_SKILLS_JSON"]) == []
    with pytest.raises(ValueError, match="disabled"):
        engine.prepare_pi_worker(config, worker, backend, "project-one", requested=["source-review"])
    assert engine.doctor(settings)["errors"] == []


def test_planning_worker_receives_contract_without_task_skill_catalog(tmp_path, monkeypatch):
    catalog, _ = make_library(tmp_path)
    config, worker, backend = local_config(tmp_path, settings_for(catalog), ["source-review"])
    monkeypatch.setattr(engine, "load_catalogs", lambda settings: pytest.fail("Planning must not load task skill packages"))
    prepared = engine.prepare_pi_worker(config, worker, backend, "project-one", planning=True)
    assert json.loads(prepared.env["BOARD_PI_SKILLS_JSON"]) == []
    assert Path(prepared.env["BOARD_PI_CONTEXT_FILE"]).is_file()


def test_missing_declared_executables_are_reported_in_doctor_and_manifest(tmp_path, monkeypatch):
    catalog, data = make_library(tmp_path)
    data["skills"][0]["requires"] = ["fixture-cli-not-installed"]
    rewrite(catalog, data)
    monkeypatch.setattr(engine.shutil, "which", lambda name: None)
    settings = settings_for(catalog)
    report = engine.doctor(settings)
    assert report["errors"] == []
    assert report["missing_declared_executables"] == ["fixture-cli-not-installed"]
    assert report["warnings"]
    config, worker, backend = local_config(tmp_path, settings)
    prepared = engine.prepare_pi_worker(config, worker, backend, "project-one")
    manifest = json.loads(Path(prepared.env["BOARD_PI_SELECTION_FILE"]).read_text(encoding="utf-8"))
    assert manifest["skills"][0]["missing_declared_executables"] == ["fixture-cli-not-installed"]
