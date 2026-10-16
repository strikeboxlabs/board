"""Trusted catalog selection and frozen, per-attempt Pi skill packages.

Catalog metadata describes knowledge. It grants no execution permissions.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html import escape
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml

from board.skill_profiles import validate_skill_profile_ids


class SkillProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(default="", max_length=1024)
    skills: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    extends: list[str] = Field(default_factory=list)

    @field_validator("extends", mode="before")
    @classmethod
    def validate_extends(cls, value):
        return validate_skill_profile_ids(value)


_BUILTIN_PROFILES = {
    "general": {"description": "Inspect supplied files, structured data and archives.",
                "tags": ["task:data-inspection", "task:archive-inspection", "task:document-inspection"]},
    "source-review": {"description": "Review supplied application source and triage static-analysis reports.",
                      "tags": ["task:source-review", "task:static-analysis", "task:finding-triage"]},
    "config-review": {"description": "Inspect configuration and diagnose host or service settings within the task scope.",
                      "tags": ["task:config-diagnostics"]},
    "offline-artifacts": {"description": "Inspect supplied binaries, cores and Windows artifacts offline.",
                          "tags": ["task:offline-analysis"]},
    "data-processing": {"description": "Inspect and transform supplied structured data, archives and documents.",
                        "tags": ["task:data-inspection", "task:archive-inspection", "task:document-inspection"]},
}
for _profile, _language in {"python": "python", "go": "go", "ruby": "ruby",
                           "javascript": "javascript", "dotnet": "csharp", "c-cpp": "c",
                           "java": "java", "php": "php"}.items():
    _BUILTIN_PROFILES[_profile] = {"description": f"Inspect {_language} source and relevant runtime/build behavior within the task scope.",
                                    "tags": [f"language:{_language}"]}
_BUILTIN_PROFILES["c-cpp"]["tags"].append("language:cpp")


class SkillsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    catalogs: list[str] = Field(default_factory=list)
    default_profiles: list[str] = Field(default_factory=lambda: ["general"])
    profiles: dict[str, SkillProfileConfig] = Field(default_factory=dict)
    max_metadata_tokens: int = Field(default=6000, ge=128, le=1000000)

    @field_validator("default_profiles", mode="before")
    @classmethod
    def validate_defaults(cls, value):
        return validate_skill_profile_ids(value)

    @model_validator(mode="after")
    def validate_profile_graph(self):
        for name in self.profiles:
            validate_skill_profile_ids([name])
        profiles = self.all_profiles()
        for name in profiles:
            self.expanded_profiles([name], profiles)
        self.expanded_profiles(self.default_profiles, profiles)
        if len(set(self.catalogs)) != len(self.catalogs):
            raise ValueError("skills.catalogs must be unique")
        return self

    def all_profiles(self) -> dict[str, SkillProfileConfig]:
        return {**{k: SkillProfileConfig.model_validate(v) for k, v in _BUILTIN_PROFILES.items()}, **self.profiles}

    @staticmethod
    def expanded_profiles(requested, profiles):
        result, visiting = [], set()
        def visit(name):
            if name not in profiles:
                raise ValueError(f"Unknown skill profile: {name}")
            if name in visiting:
                raise ValueError(f"Skill profile inheritance cycle at {name}")
            if name in result:
                return
            visiting.add(name)
            for parent in profiles[name].extends:
                visit(parent)
            visiting.remove(name)
            result.append(name)
        for name in requested:
            visit(name)
        return result


@dataclass(frozen=True)
class CatalogSkill:
    id: str
    name: str
    description: str
    directory: Path
    tags: tuple[str, ...]
    requires: tuple[str, ...]
    digest: str
    catalog_digest: str


def _read_json(path: Path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key {key} in {path}")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs)


def package_digest(directory: Path) -> str:
    """Hash all regular resources and reject links that could escape a frozen package."""
    digest = sha256()
    files = sorted(directory.rglob("*"))
    for path in files:
        if path.is_symlink():
            raise ValueError(f"Skill resource symlinks are unsupported: {path}")
        if path.is_file():
            relative = path.relative_to(directory).as_posix().encode()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return digest.hexdigest()


def load_catalogs(settings: SkillsConfig) -> dict[str, CatalogSkill]:
    result, seen_names, seen_libraries = {}, {}, set()
    for configured in settings.catalogs:
        catalog = Path(configured).expanduser().resolve(strict=True)
        root = catalog.parent
        data = _read_json(catalog)
        if (not isinstance(data, dict) or set(data) != {"schema_version", "library_id", "skills"}
                or type(data.get("schema_version")) is not int or data["schema_version"] != 1):
            raise ValueError(f"Unsupported skill catalog: {catalog}")
        library = data.get("library_id")
        validate_skill_profile_ids([library])
        if library in seen_libraries:
            raise ValueError(f"Duplicate skill library: {library}")
        seen_libraries.add(library)
        entries = data.get("skills")
        if not isinstance(entries, list):
            raise ValueError(f"Catalog skills must be an array: {catalog}")
        catalog_digest = sha256(catalog.read_bytes()).hexdigest()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"id", "path", "tags", "requires"}:
                raise ValueError(f"Invalid catalog entry in {catalog}")
            name = entry["id"]
            validate_skill_profile_ids([name])
            if entry["path"] != f"skills/{name}":
                raise ValueError(f"Catalog path must be skills/{name}")
            relative = PurePosixPath(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or "\\" in entry["path"]:
                raise ValueError(f"Unsafe skill path: {entry['path']}")
            directory = (root / entry["path"]).resolve(strict=True)
            if not directory.is_relative_to(root) or directory == root:
                raise ValueError(f"Skill escapes catalog root: {directory}")
            entrypoint = directory / "SKILL.md"
            if entrypoint.is_symlink() or not entrypoint.resolve().is_relative_to(directory):
                raise ValueError(f"Skill entrypoint escapes its package: {entrypoint}")
            raw = entrypoint.read_text(encoding="utf-8-sig")
            match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", raw, re.S)
            if not match:
                raise ValueError(f"Missing skill frontmatter: {directory}")
            frontmatter = yaml.safe_load(match.group(1))
            if not isinstance(frontmatter, dict) or frontmatter.get("name") != name:
                raise ValueError(f"Catalog/skill name mismatch: {directory}")
            description = frontmatter.get("description")
            if not isinstance(description, str) or not description.strip() or len(description) > 1024:
                raise ValueError(f"Missing/invalid skill description: {directory}")
            for field in ("tags", "requires"):
                values = entry[field]
                if (not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() for x in values)
                        or len(set(values)) != len(values)):
                    raise ValueError(f"Invalid {field} for {library}/{name}")
            tag_pattern = re.compile(r"(?:task|execution|target|language|artifact):[a-z0-9]+(?:-[a-z0-9]+)*\Z")
            if any(tag != "classification:unreviewed" and not tag_pattern.fullmatch(tag) for tag in entry["tags"]):
                raise ValueError(f"Invalid tag facet for {library}/{name}")
            if "classification:unreviewed" in entry["tags"]:
                if "execution:linux" not in entry["tags"] or any(t.startswith("task:") for t in entry["tags"]):
                    raise ValueError(f"Unreviewed classification claims unsupported tasks: {library}/{name}")
            if any(value != value.strip() for value in entry["requires"]):
                raise ValueError(f"Invalid trimmed requirement for {library}/{name}")
            identity = f"{library}/{name}"
            if identity in result or name in seen_names:
                raise ValueError(f"Skill name collision: {identity} and {seen_names.get(name, identity)}")
            seen_names[name] = identity
            result[identity] = CatalogSkill(identity, name, description, directory,
                tuple(entry["tags"]), tuple(entry["requires"]), package_digest(directory), catalog_digest)
    return result


def resolve_skills(settings: SkillsConfig, catalog: dict[str, CatalogSkill], requested: list[str]):
    requested = validate_skill_profile_ids(requested)
    if not settings.enabled:
        if requested:
            raise ValueError("Intent requests skill profiles, but skills.enabled is false")
        return (), ()
    profiles = settings.all_profiles()
    expanded = settings.expanded_profiles(requested or settings.default_profiles, profiles)
    selected = {}
    for profile_id in expanded:
        profile = profiles[profile_id]
        for identity in profile.skills:
            if identity not in catalog:
                raise ValueError(f"Profile {profile_id} references missing skill {identity}")
            selected[identity] = catalog[identity]
        for identity, skill in catalog.items():
            if "classification:unreviewed" not in skill.tags and set(profile.tags).intersection(skill.tags):
                selected[identity] = skill
    return tuple(expanded), tuple(selected[k] for k in sorted(selected))


def metadata_characters(skills, paths=None):
    # Pi 1.3 XML entries, including path/escaping; tokens below remain an estimate.
    parts = []
    for index, skill in enumerate(skills):
        location = str(paths[index] / "SKILL.md") if paths else str(skill.directory / "SKILL.md")
        parts.append("  <skill>\n    <name>" + escape(skill.name, quote=True)
            + "</name>\n    <description>" + escape(skill.description, quote=True)
            + "</description>\n    <location>" + escape(location, quote=True)
            + "</location>\n  </skill>")
    return len("\n".join(parts)) + (500 if parts else 0)


def profile_summaries(settings: SkillsConfig) -> str:
    if not settings.enabled:
        return "Skill profiles are disabled on this dispatcher. Omit skill_profiles."
    catalog = load_catalogs(settings)
    lines = ["Available trusted skill profiles (knowledge selection; task scope still applies):"]
    for name, profile in settings.all_profiles().items():
        _, skills = resolve_skills(settings, catalog, [name])
        if skills:
            lines.append(f"- {name}: {profile.description} ({len(skills)} skills)")
    lines.append("Request only applicable IDs in skill_profiles; combine profiles for mixed tasks. "
                 "Profiles are delivered to Pi workers. A missing profile is a coverage gap, not proof of absence.")
    return "\n".join(lines)


def doctor(settings: SkillsConfig) -> dict[str, Any]:
    report: dict[str, Any] = {"enabled": settings.enabled, "catalogs": settings.catalogs,
                              "profiles": [], "warnings": [], "errors": []}
    if not settings.enabled:
        if settings.catalogs:
            report["warnings"].append("Catalogs configured but skill loading is disabled")
        return report
    try:
        catalog = load_catalogs(settings)
        report["catalog_skill_count"] = len(catalog)
        report["unreviewed_classifications"] = sum("classification:unreviewed" in x.tags for x in catalog.values())
        report["requirements_coverage"] = {"declared": sum(bool(x.requires) for x in catalog.values()),
                                           "unspecified": sum(not x.requires for x in catalog.values())}
        for name, profile in settings.all_profiles().items():
            _, skills = resolve_skills(settings, catalog, [name])
            estimate = math.ceil(metadata_characters(skills) / 4)
            report["profiles"].append({"id": name, "description": profile.description,
                                       "skills": len(skills), "estimated_metadata_tokens": estimate})
        _, defaults = resolve_skills(settings, catalog, [])
        if settings.catalogs == []:
            report["errors"].append("Skill loading is enabled without any catalogs")
        if math.ceil(metadata_characters(defaults) / 4) > settings.max_metadata_tokens:
            report["errors"].append("Default profiles exceed skills.max_metadata_tokens; split the selection or raise the explicit budget")
        missing = sorted({command for skill in catalog.values() for command in skill.requires if not shutil.which(command)})
        report["missing_declared_executables"] = missing
        if missing:
            report["warnings"].append("Some declared executables are unavailable; skill discovery does not establish tool readiness")
        report["warnings"].append("Metadata tokens are estimated as characters / 4; package discovery is not a model effectiveness test")
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        report["errors"].append(str(exc))
    return report


def prepare_pi_worker(config, worker, backend, project_id: str, intent_id: str | None = None,
                      requested: list[str] | None = None, *, planning: bool = False):
    """Return a private WorkerConfig copy; never mutate shared driver/config state."""
    requested = requested or []
    if worker.type != "pi":
        if requested or worker.skill_profiles:
            raise ValueError("Skill profiles currently require a Pi execution worker")
        return worker
    if config.runtime.execution != "local":
        if requested or config.skills.enabled:
            raise ValueError("Catalog skill profiles currently require local execution")
        return worker
    env = dict(worker.env)
    project_dir = Path(backend.project_dir(project_id))
    if config.local.pi_session_storage == "project":
        env["BOARD_PI_SESSION_DIR"] = str(project_dir / "sessions" / "pi")
    else:
        legacy_root = worker.env.get("PI_AGENT_DIR") or "/tmp/board-pi"
        env["BOARD_PI_SESSION_DIR"] = str(PurePosixPath(legacy_root) / worker.name / "sessions")
    attempt = uuid.uuid4().hex
    directory = Path(backend.scratch_dir(project_id)) / "skill-attempts" / attempt
    profiles, skills = (), ()
    if not planning and config.skills.enabled:
        profiles, skills = resolve_skills(config.skills, load_catalogs(config.skills), requested or worker.skill_profiles)
    elif (requested or worker.skill_profiles) and not planning:
        raise ValueError("Intent requests profiles, but skills are disabled")
    destinations = tuple(directory / "packages" / skill.id for skill in skills)
    characters = metadata_characters(skills, destinations)
    estimate = math.ceil(characters / 4)
    if estimate > config.skills.max_metadata_tokens:
        raise ValueError(f"Selected profiles advertise approximately {estimate} metadata tokens, exceeding "
                         f"the configured {config.skills.max_metadata_tokens}; split the intent or adjust the explicit budget")
    directory.mkdir(parents=True)
    for skill, destination in zip(skills, destinations):
        shutil.copytree(skill.directory, destination, symlinks=True)
        if package_digest(destination) != skill.digest or package_digest(skill.directory) != skill.digest:
            raise ValueError(f"Skill package changed during snapshot: {skill.id}")
    env["BOARD_PI_SKILLS_JSON"] = json.dumps([str(p) for p in destinations])
    guide = Path(__file__).parent / "prompts" / "default" / "AGENTS.workspace.md"
    guide_text = guide.read_text(encoding="utf-8")
    guide_text += "\n\nSelected skill profiles: " + (", ".join(profiles) or "none") + ".\n"
    guide_text += "Selected skills are knowledge resources, not execution permission. Read relevant SKILL.md "
    guide_text += "and its required references before using the guidance. Report unavailable prerequisites, "
    guide_text += "missing capabilities and coverage gaps. Record candidate state and evidence for the next intent.\n"
    guide_path = directory / "board-context.md"
    guide_path.write_text(guide_text, encoding="utf-8", newline="\n")
    env["BOARD_PI_CONTEXT_FILE"] = str(guide_path)
    manifest = {"schema_version": 1, "attempt_id": attempt, "project_id": project_id, "intent_id": intent_id,
        "worker": worker.name, "planning": planning, "profiles": list(profiles), "skill_count": len(skills),
        "metadata_characters": characters, "estimated_metadata_tokens": estimate,
        "token_estimation": "characters/4; not measured model tokens", "context_sha256": sha256(guide_text.encode()).hexdigest(),
        "skills": [{"id": skill.id, "source": str(skill.directory), "snapshot": str(destination),
                    "package_sha256": skill.digest, "catalog_sha256": skill.catalog_digest,
                    "requirements_status": "declared" if skill.requires else "unspecified",
                    "missing_declared_executables": [x for x in skill.requires if not shutil.which(x)]}
                   for skill, destination in zip(skills, destinations)]}
    evidence = Path(backend.evidence_dir(project_id, intent_id))
    manifest_path = evidence / f"skill-selection-{attempt}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    env["BOARD_PI_SELECTION_FILE"] = str(manifest_path)
    return worker.model_copy(update={"env": env, **({"pi_extensions": []} if planning else {})})
