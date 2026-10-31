"""Validation shared by file configuration, the settings API, and dispatch."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

TASK_TYPES = ("bootstrap", "reason", "explore")
MAX_REASON_TIMEOUT = 3600
MAX_EXECUTION_TIMEOUT = 86400
MAX_CONCLUDE_TIMEOUT = 3600
MAX_HEARTBEAT_TIMEOUT = 86400
MAX_WORKERS = 256


class DispatchOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    routing: dict[Literal["reason", "explore", "bootstrap"], list[str]] = Field(default_factory=dict)
    worker_max_running: dict[str, Annotated[int, Field(gt=0, le=MAX_WORKERS, strict=True)]] = Field(default_factory=dict)
    reason_execution_timeout: int | None = Field(default=None, gt=0, le=MAX_REASON_TIMEOUT, strict=True)
    explore_execution_timeout: int | None = Field(default=None, gt=0, le=MAX_EXECUTION_TIMEOUT, strict=True)

    @field_validator("routing")
    @classmethod
    def one_worker_per_task(cls, value):
        for task, names in value.items():
            if len(names) != 1 or not names[0].strip():
                raise ValueError(f"Choose exactly one configured worker for {task}")
        return value


def validation_messages(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
        for error in exc.errors(include_input=False, include_context=False, include_url=False)
    ]


def validate_dispatch_overrides(overrides: DispatchOverrides, context: dict | None) -> None:
    """Check worker references and effective limits using a dispatcher report.

    The dispatcher supplies its current file config directly; the API uses the
    latest report. Dispatch revalidates every tick if the file changes afterward.
    """
    referenced = set(overrides.worker_max_running) | {name for names in overrides.routing.values() for name in names}
    if not context:
        if referenced:
            raise ValueError("Wait for the dispatcher to report its configured workers before selecting a model")
        return
    defaults = context.get("file_defaults", context)
    workers = {worker["name"]: worker for worker in defaults["workers"]}
    unknown = referenced - workers.keys()
    if unknown:
        raise ValueError(f"Workers are not configured: {', '.join(sorted(unknown))}")
    for name in referenced:
        if not workers[name].get("available", True):
            raise ValueError(f"{name} is unavailable: {workers[name].get('unavailable_reason') or 'not configured'}")
    maximum = defaults["runtime"]["max_workers"]
    for name, count in overrides.worker_max_running.items():
        if count > maximum:
            raise ValueError(f"{name} concurrency must be between 1 and the global limit ({maximum})")
    for task in TASK_TYPES:
        names = overrides.routing.get(task, [name for name, worker in workers.items() if task in worker["task_types"]])
        if len(names) > 1:
            raise ValueError(f"Choose exactly one configured worker for {task}")
        for name in names:
            if not workers[name].get("available", True):
                reason = workers[name].get("unavailable_reason") or "not configured"
                raise ValueError(f"{name} is unavailable for {task}: {reason}")


def validate_heartbeat_timeouts(intent_timeout: int, reason_timeout: int, context: dict | None) -> None:
    if context:
        interval = context.get("heartbeat_interval") or context["runtime"]["interval"]
        if min(intent_timeout, reason_timeout) <= interval:
            raise ValueError(f"Heartbeat leases must be longer than the worker heartbeat interval ({interval} seconds)")


def settings_limits(context: dict | None) -> dict:
    return {
        "reason_execution_timeout": MAX_REASON_TIMEOUT,
        "explore_execution_timeout": MAX_EXECUTION_TIMEOUT,
        "heartbeat_timeout": MAX_HEARTBEAT_TIMEOUT,
        "worker_max_running": context.get("file_defaults", context)["runtime"]["max_workers"] if context else MAX_WORKERS,
        "heartbeat_minimum": max(5, (context.get("heartbeat_interval") or context["runtime"]["interval"]) + 1) if context else 5,
    }
