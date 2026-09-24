from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from board.dispatcher.config import LocalConfig
from board.dispatcher.runtime.local_process import LocalProcess
from board.reporting import paths as reporting_paths

LOG = logging.getLogger(__name__)


class LocalBackend:
    """Runs workers directly on the dispatcher host instead of in per-project containers.

    Each project gets a durable directory under the projects root (``evidence_root``
    config key, falling back to legacy ``workspace_root``, falling back to
    ``BOARD_PROJECTS_DIR`` or ``/data/strikebox-data``)::

        <root>/<project_id>/
          evidence/<intent_id>/   # worker artefacts, referenced by $BOARD_EVIDENCE_DIR
          workspace/              # worker cwd
          reports/                # persisted timeline / graph / reports
          scratch/                # graph snapshots and other regenerable internals

    Worker processes inherit the host environment so the pre-configured ``claude`` /
    ``codex`` / ``pi`` CLIs and their credentials are used as-is; no API keys are
    injected. There are no containers to build or tear down, so the container-lifecycle
    methods are inert.
    """

    def __init__(self, config: LocalConfig):
        self._config = config
        root = config.evidence_root or config.workspace_root
        self._root = Path(root).expanduser() if root else reporting_paths.default_projects_root()

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        return str(self._project_dir(project_id))

    def ensure_running(self, project_id: str) -> str:
        project_dir = self._project_dir(project_id)
        for child in ("workspace", "evidence", "reports", "scratch"):
            (project_dir / child).mkdir(parents=True, exist_ok=True)
        self._seed_workspace_agents_md(project_dir / "workspace")
        LOG.debug("local project workdir ready project=%s dir=%s", project_id, project_dir)
        return str(project_dir)

    @staticmethod
    def _seed_workspace_agents_md(workspace: Path) -> None:
        """Seed an AGENTS.md runbook into the workspace (pi loads it each session).

        Copied only when absent — edit the file in place to customise a project;
        it is never overwritten.
        """
        target = workspace / "AGENTS.md"
        if target.exists():
            return
        try:
            template = (
                Path(__file__).resolve().parent.parent
                / "prompts" / "default" / "AGENTS.workspace.md"
            ).read_text(encoding="utf-8")
            target.write_text(template, encoding="utf-8")
        except OSError:
            LOG.debug("AGENTS.md seed skipped (template unreadable or workspace unwritable)")

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> LocalProcess:
        merged_env = {**os.environ, **(env or {})}
        workspace = Path(container_name) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return LocalProcess(
            command,
            cwd=str(workspace),
            env=merged_env,
            timeout_seconds=timeout_seconds,
            term_grace_seconds=kill_after_seconds,
        )

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError(f"local file path must be absolute: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return self._config.completed_action == "remove" and self._project_dir(project_id).exists()

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return False

    def cleanup_completed(self, project_id: str) -> bool:
        if self._config.completed_action == "remove":
            project_dir = self._project_dir(project_id)
            LOG.info("removing completed project workdir project=%s dir=%s", project_id, project_dir)
            shutil.rmtree(project_dir, ignore_errors=True)
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        return True

    def managed_container_names(self) -> list[str]:
        return []

    def project_dir(self, project_id: str) -> Path:
        directory = self._project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def evidence_dir(self, project_id: str, intent_id: str | None = None) -> Path:
        directory = reporting_paths.intent_evidence_dir(self._root, project_id, intent_id) if intent_id else reporting_paths.evidence_dir(self._root, project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def reports_dir(self, project_id: str) -> Path:
        directory = reporting_paths.reports_dir(self._root, project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def scratch_dir(self, project_id: str) -> str:
        directory = reporting_paths.scratch_dir(self._root, project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    def worker_env(self, project_id: str, intent_id: str | None = None) -> dict[str, str]:
        return {
            "BOARD_PROJECT_DIR": str(self.project_dir(project_id)),
            "BOARD_EVIDENCE_DIR": str(self.evidence_dir(project_id, intent_id)),
        }

    def evidence_relative_path(self, project_id: str, intent_id: str | None, file_path: Path) -> str:
        evidence_dir = reporting_paths.intent_evidence_dir(self._root, project_id, intent_id) if intent_id else reporting_paths.evidence_dir(self._root, project_id)
        relative = file_path.relative_to(evidence_dir).as_posix()
        stored = reporting_paths.evidence_relative_path(self._root, project_id, intent_id, relative)
        return stored

    def _project_dir(self, project_id: str) -> Path:
        return reporting_paths.project_dir(self._root, project_id)
