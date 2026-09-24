"""Canonical durable layout for per-project files.

Every project owns a directory under the projects root::

    <projects_root>/<project_id>/
      evidence/<intent_id>/...   # durable worker artefacts (captures, screenshots, ...)
      workspace/                 # per-project working directory (worker cwd)
      reports/                   # rendered timeline / graph / reports land here
      scratch/                   # regenerable internals (graph snapshots, ...)

The projects root defaults to ``/data/strikebox-data`` and can be overridden with the
``BOARD_PROJECTS_DIR`` environment variable or the ``evidence_root``
config key in the dispatcher config.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECTS_ROOT_ENV = "BOARD_PROJECTS_DIR"


def default_projects_root() -> Path:
    override = os.environ.get(PROJECTS_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return Path("/data/strikebox-data")


def sanitize_project_id(project_id: str) -> str:
    return project_id.replace("/", "-")


def project_dir(projects_root: Path | str, project_id: str) -> Path:
    return Path(projects_root).expanduser() / sanitize_project_id(project_id)


def evidence_dir(projects_root: Path | str, project_id: str) -> Path:
    return project_dir(projects_root, project_id) / "evidence"


def intent_evidence_dir(projects_root: Path | str, project_id: str, intent_id: str) -> Path:
    return evidence_dir(projects_root, project_id) / sanitize_project_id(intent_id)


def workspace_dir(projects_root: Path | str, project_id: str) -> Path:
    return project_dir(projects_root, project_id) / "workspace"


def reports_dir(projects_root: Path | str, project_id: str) -> Path:
    return project_dir(projects_root, project_id) / "reports"


def scratch_dir(projects_root: Path | str, project_id: str) -> Path:
    return project_dir(projects_root, project_id) / "scratch"


def evidence_relative_path(projects_root: Path | str, project_id: str, intent_id: str | None, name: str) -> str:
    """Canonical evidence row path: relative to the projects root, POSIX separators."""
    parts = [sanitize_project_id(project_id), "evidence"]
    if intent_id:
        parts.append(sanitize_project_id(intent_id))
    parts.append(name)
    return "/".join(parts)
