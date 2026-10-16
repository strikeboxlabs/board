from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from board.skill_profiles import validate_skill_profile_ids

IntentWorkerType = Literal["pi", "codex", "claudecode", "mock"]


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


class Fact(BaseModel):
    id: str
    description: str


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    priority: int = 50
    blocked_by: str | None = None
    assigned_worker: str | None = None
    assigned_worker_type: IntentWorkerType | None = None
    skill_profiles: list[str] = Field(default_factory=list)
    dispatch_error: str | None = None
    dispatch_error_at: str | None = None
    dispatch_error_worker: str | None = None
    cancel_requested_at: str | None = None
    cancelled_at: str | None = None
    cancelled_by: str | None = None
    priority_set_by: str | None = None
    priority_set_at: str | None = None
    excluded: bool = False

    model_config = {"populate_by_name": True}

    @field_validator("skill_profiles", mode="before")
    @classmethod
    def validate_profiles(cls, value):
        return validate_skill_profile_ids(value)


class UpdateIntentPriorityRequest(BaseModel):
    priority: int = Field(ge=1, le=100)
    actor: str = "human"

    @field_validator("actor")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class UpdateIntentAssignmentRequest(BaseModel):
    assigned_worker: str | None = None
    assigned_worker_type: IntentWorkerType | None = None
    skill_profiles: list[str] = Field(default_factory=list)
    actor: str = "human"

    @field_validator("skill_profiles", mode="before")
    @classmethod
    def validate_profiles(cls, value):
        return validate_skill_profile_ids(value)

    @field_validator("assigned_worker", "actor")
    @classmethod
    def validate_optional_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        return text


class UpdateIntentSkillProfilesRequest(BaseModel):
    skill_profiles: list[str]
    actor: str = Field(default="human", min_length=1, max_length=200)

    @field_validator("skill_profiles", mode="before")
    @classmethod
    def validate_profiles(cls, value):
        return validate_skill_profile_ids(value)


class FailIntentRequest(BaseModel):
    worker: str | None = None
    error: str = Field(min_length=1, max_length=2000)

    @field_validator("error")
    @classmethod
    def validate_error(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("error must not be empty")
        return text


class CancelIntentRequest(BaseModel):
    actor: str = Field(default="human", min_length=1, max_length=200)


JobStatus = Literal["running", "hit", "done", "exhausted", "failed", "killed"]
JOB_TERMINAL_STATUSES = frozenset({"hit", "done", "exhausted", "failed", "killed"})


class Job(BaseModel):
    id: str
    project_id: str
    description: str
    status: JobStatus
    check_command: str | None = None
    stop_command: str | None = None
    budget_seconds: int | None = None
    creator: str
    last_check_at: str | None = None
    last_output: str | None = None
    fact_id: str | None = None
    created_at: str
    finished_at: str | None = None
    stop_state: Literal["pending", "stopped", "failed", "manual"] | None = None
    stop_error: str | None = None


class CreateJobRequest(BaseModel):
    description: str
    check_command: str | None = None
    stop_command: str | None = None
    budget_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    creator: str

    @field_validator("description", "creator", "check_command", "stop_command")
    @classmethod
    def validate_optional_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        return text

    @field_validator("description", "creator")
    @classmethod
    def validate_required_non_empty(cls, value: str | None) -> str:
        if value is None or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class JobProgressRequest(BaseModel):
    worker: str
    output: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class FinishJobRequest(BaseModel):
    worker: str
    status: JobStatus
    result: str = ""

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("status")
    @classmethod
    def validate_terminal_status(cls, value: str) -> str:
        if value not in JOB_TERMINAL_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(JOB_TERMINAL_STATUSES))}")
        return value


class KillJobRequest(BaseModel):
    worker: str = "human"
    actor: str | None = None
    reason: str = ""

    @field_validator("worker", "actor")
    @classmethod
    def validate_optional_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        return text


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


ProjectStatus = Literal["active", "stopped", "completed", "aborted"]


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: ProjectStatus
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None
    abort_reason: str | None = None
    aborted_at: str | None = None
    aborted_by: str | None = None
    suspension_id: str | None = None
    suspension_ack_id: str | None = None
    execution_notes: str = ""


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]
    jobs: list[Job] = Field(default_factory=list)


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    skill_profiles: list[str] = Field(default_factory=list)
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None
    priority: int = Field(default=50, ge=1, le=100)
    blocked_by: str | None = None
    assigned_worker: str | None = None
    assigned_worker_type: IntentWorkerType | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker", "blocked_by", "assigned_worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("skill_profiles", mode="before")
    @classmethod
    def validate_profiles(cls, value):
        return validate_skill_profile_ids(value)

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]
    execution_notes: str | None = Field(default=None, max_length=12000)
    excluded_intent_ids: list[str] | None = None
    confirmed_stopped_job_ids: list[str] = Field(default_factory=list)


class SuspensionAckRequest(BaseModel):
    suspension_id: str


class JobStopResultRequest(BaseModel):
    success: bool
    error: str = Field(default="", max_length=2000)


class AbortRequest(BaseModel):
    reason: str
    actor: str | None = None

    @field_validator("reason", "actor")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent


EvidenceKind = Literal["screenshot", "capture", "script", "data", "notes", "lost"]
FindingSeverity = Literal["critical", "high", "medium", "low", "info", "negative"]
FindingStatus = Literal["open", "confirmed", "mitigated", "false_positive", "needs_retest", "blocked"]

FINDING_SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
    "negative": 5,
}


class Evidence(BaseModel):
    id: str
    intent_id: str | None = None
    fact_id: str | None = None
    path: str
    kind: EvidenceKind | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    created_at: str
    description: str | None = None


class RegisterEvidenceRequest(BaseModel):
    path: str
    kind: EvidenceKind | None = None
    intent_id: str | None = None
    fact_id: str | None = None
    description: str | None = None
    sha256: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class Finding(BaseModel):
    id: str
    fact_id: str
    intent_id: str | None = None
    title: str
    category: str | None = None
    severity: FindingSeverity | None = None
    status: FindingStatus | None = None
    affected_target: str | None = None
    description: str
    recommendation: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class CreateFindingRequest(BaseModel):
    fact_id: str
    title: str
    intent_id: str | None = None
    category: str | None = None
    severity: FindingSeverity = "info"
    status: FindingStatus = "open"
    affected_target: str | None = None
    description: str
    recommendation: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("title", "description", "fact_id")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class UpdateFindingRequest(BaseModel):
    title: str | None = None
    category: str | None = None
    severity: FindingSeverity | None = None
    status: FindingStatus | None = None
    affected_target: str | None = None
    recommendation: str | None = None
    evidence_ids: list[str] | None = None


def finding_row_to_model(row) -> Finding:
    raw_ids = row["evidence_ids"]
    evidence_ids: list[str] = []
    if raw_ids:
        try:
            parsed = json.loads(raw_ids)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            evidence_ids = [str(item) for item in parsed]
    return Finding(
        id=row["id"],
        fact_id=row["fact_id"],
        intent_id=row["intent_id"],
        title=row["title"],
        category=row["category"],
        severity=row["severity"],
        status=row["status"],
        affected_target=row["affected_target"],
        description=row["description"],
        recommendation=row["recommendation"],
        evidence_ids=evidence_ids,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
