from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from board.dispatcher.runtime.process import ExecProcess


@runtime_checkable
class ExecutionBackend(Protocol):
    """The execution substrate for worker processes.

    Two implementations exist: ContainerManager (one Docker container per project) and
    LocalBackend (host subprocesses, one working directory per project). The scheduler,
    task runners and startup healthcheck only depend on this surface so the two backends
    are interchangeable behind a single ``runtime.execution`` switch.

    Both backends also implement the durable evidence contract: every project owns a
    directory under the configured projects root (``evidence/<intent>/`` for worker
    artefacts, ``workspace/`` as the worker cwd, ``reports/`` for rendered output,
    ``scratch/`` for regenerable internals). ``worker_env`` exposes those locations to
    worker processes via ``BOARD_PROJECT_DIR`` / ``BOARD_EVIDENCE_DIR`` so evidence
    survives reboots instead of landing in self-chosen ``/tmp`` paths.
    """

    def container_name(self, project_id: str) -> str: ...

    def ensure_running(self, project_id: str) -> str: ...

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> ExecProcess: ...

    def write_text_file(self, container_name: str, path: str, content: str) -> None: ...

    def needs_completed_cleanup(self, project_id: str) -> bool: ...

    def needs_stopped_cleanup(self, project_id: str) -> bool: ...

    def cleanup_completed(self, project_id: str) -> bool: ...

    def cleanup_stopped(self, project_id: str) -> bool: ...

    def project_dir(self, project_id: str) -> Path:
        """Host-side durable directory for the project (creates it)."""
        ...

    def evidence_dir(self, project_id: str, intent_id: str | None = None) -> Path:
        """Host-side evidence directory for a project or intent (creates it)."""
        ...

    def reports_dir(self, project_id: str) -> Path:
        """Host-side directory where rendered reports are persisted (creates it)."""
        ...

    def scratch_dir(self, project_id: str) -> str:
        """Execution-context scratch directory (regenerable internals, e.g. graph snapshots)."""
        ...

    def worker_env(self, project_id: str, intent_id: str | None = None) -> dict[str, str]:
        """Env vars pointing workers at their durable project/evidence directories.

        Values are absolute in the *execution* context: host paths for local mode,
        in-container paths for container mode.
        """
        ...

    def evidence_relative_path(self, project_id: str, intent_id: str | None, file_path: Path) -> str:
        """Stored evidence row path for a file inside the intent evidence dir."""
        ...

    def close(self) -> None: ...
