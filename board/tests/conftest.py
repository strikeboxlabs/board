from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tempfile

from board.dispatcher.config import DispatchConfig
from board.dispatcher.protocol.client import ApiResult
from board.dispatcher.workers.base import DriverResult
from board.dispatcher.workers.health import HealthResult
from board.server.models import Fact, Hint, Intent, ProjectDetail, ProjectMeta


def make_config() -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "http://127.0.0.1:8000",
            "runtime": {
                "interval": 60,
                "max_workers": 2,
                "max_running_projects": 1,
                "max_project_workers": 2,
                "healthcheck_timeout": 5,
                "prompt_group": "default",
            },
            "tasks": {
                "bootstrap": {"timeout": 10, "conclude_timeout": 5},
                "reason": {"timeout": 10, "max_intents": 3},
                "explore": {"timeout": 10, "conclude_timeout": 5},
            },
            "container": {
                "image": "test-image",
                "network_mode": "host",
                "completed_action": "stop",
                "evidence_root": "/tmp/board-test-projects",
            },
            "workers": [
                {
                    "name": "test-worker",
                    "type": "mock",
                    "task_types": ["bootstrap", "reason", "explore"],
                    "max_running": 1,
                    "priority": 0,
                }
            ],
        }
    )


def make_project(*, intents: list[Intent] | None = None) -> ProjectDetail:
    return ProjectDetail(
        project=ProjectMeta(
            id="proj_001",
            title="test",
            status="active",
            bootstrap_enabled=True,
            created_at="2026-01-01T00:00:00Z",
        ),
        facts=[
            Fact(id="origin", description="start"),
            Fact(id="goal", description="finish"),
            Fact(id="f001", description="known fact"),
        ],
        intents=intents or [],
        hints=[
            Hint(
                id="h001",
                content="use the clue",
                creator="human",
                created_at="2026-01-01T00:00:01Z",
            )
        ],
    )


def make_intent(intent_id: str = "i001") -> Intent:
    return Intent(
        id=intent_id,
        from_=["f001"],
        description="investigate",
        creator="reasoner",
        worker="test-worker",
        created_at="2026-01-01T00:00:02Z",
    )


class FakeLease:
    def __init__(self) -> None:
        self.failure = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def attach_process(self, _process) -> None:
        return None


@dataclass
class FakeContainerManager:
    writes: list[tuple[str, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="board-fake-backend-"))

    def ensure_running(self, project_id: str) -> str:
        return f"container-{project_id}"

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.writes.append((container_name, path, content))

    def project_dir(self, project_id: str) -> Path:
        directory = self.root / project_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def scratch_dir(self, project_id: str) -> str:
        directory = self.root / project_id / "scratch"
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    def worker_env(self, project_id: str, intent_id: str | None = None) -> dict[str, str]:
        return {
            "BOARD_PROJECT_DIR": str(self.root / project_id),
            "BOARD_EVIDENCE_DIR": str(self.evidence_dir(project_id, intent_id)),
        }

    def evidence_dir(self, project_id: str, intent_id: str | None = None) -> Path:
        directory = self.root / project_id / "evidence" / (intent_id or "")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def reports_dir(self, project_id: str) -> Path:
        directory = self.root / project_id / "reports"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def evidence_relative_path(self, project_id: str, intent_id: str | None, file_path: Path) -> str:
        return f"{project_id}/evidence/{intent_id}/{file_path.name}"


@dataclass
class FakeClient:
    project: ProjectDetail
    concluded: list[tuple[str, str, str, str]] = field(default_factory=list)
    completed: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    created_intents: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    requested_skill_profiles: list[list[str]] = field(default_factory=list)
    released: list[tuple[str, str, str]] = field(default_factory=list)
    released_reasons: list[tuple[str, str]] = field(default_factory=list)
    registered_evidence: list[tuple[str, dict]] = field(default_factory=list)
    created_findings: list[tuple[str, dict]] = field(default_factory=list)

    def get_project(self, _project_id: str) -> ProjectDetail:
        return self.project

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        self.concluded.append((project_id, intent_id, worker, description))
        return ApiResult(200, {"fact": {"id": "f002"}})

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        self.completed.append((project_id, from_ids, description, worker))
        return ApiResult(200, {})

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str, priority: int | None = None, blocked_by: str | None = None, skill_profiles: list[str] | None = None) -> ApiResult:
        self.created_intents.append((project_id, from_ids, description, creator))
        self.requested_skill_profiles.append(list(skill_profiles or []))
        return ApiResult(201, {})

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        self.released.append((project_id, intent_id, worker))
        return ApiResult(200, {})

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        self.released_reasons.append((project_id, worker))
        return ApiResult(200, {})

    def heartbeat(self, _project_id: str, _intent_id: str, _worker: str) -> ApiResult:
        return ApiResult(200, {})

    def reason_heartbeat(self, _project_id: str, _worker: str) -> ApiResult:
        return ApiResult(200, {})

    def register_evidence(self, project_id: str, payload: dict) -> ApiResult:
        self.registered_evidence.append((project_id, payload))
        return ApiResult(201, {})

    def create_finding(self, project_id: str, payload: dict) -> ApiResult:
        self.created_findings.append((project_id, payload))
        return ApiResult(201, {})

    def export_project(self, project_id: str, format: str = "yaml") -> str:
        return f"# export {project_id} format={format}\n"

    def get_report(self, project_id: str, report_type: str) -> ApiResult:
        return ApiResult(200, text=f"# report {project_id} type={report_type}\n")

    def get_asset_map(self, project_id: str, format: str) -> ApiResult:
        return ApiResult(200, text=f"# asset map {project_id} format={format}\n")


class FakeDriver:
    def __init__(self) -> None:
        self.execute_prompts: list[str] = []
        self.conclude_prompts: list[str] = []
        self.health = HealthResult(ok=True, status=200, detail="")

    def supports_conclude(self) -> bool:
        return True

    def prepare_session(self) -> str:
        return "session-001"

    def check_health(self, _worker, *, timeout: float) -> HealthResult:
        return self.health

    def build_execute(self, _worker, prompt: str, session: str | None) -> DriverResult:
        self.execute_prompts.append(prompt)
        return DriverResult(["execute"], session=session)

    def build_conclude(self, _worker, prompt: str, _session: str) -> list[str]:
        self.conclude_prompts.append(prompt)
        return ["conclude"]

    def extract_session(self, session: str | None, _stdout: str, _stderr: str) -> str | None:
        return session

    def extract_response_text(self, stdout: str, _stderr: str) -> str:
        return stdout
