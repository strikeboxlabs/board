import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from board.dispatch_settings import (settings_limits, validate_dispatch_overrides,
                                     validate_heartbeat_timeouts, validation_messages)
from board.server.db import get_conn, get_read_conn
from board.server.models import DispatchOverrides, Settings, SettingsSnapshot

router = APIRouter(tags=["settings"])


def _read_settings(conn) -> dict:
    row = conn.execute("SELECT intent_timeout, reason_timeout, dispatch FROM settings WHERE rowid = 1").fetchone()
    return dict(intent_timeout=row["intent_timeout"], reason_timeout=row["reason_timeout"], dispatch=json.loads(row["dispatch"]))


def _context(conn) -> dict | None:
    row = conn.execute("SELECT payload FROM dispatch_effective WHERE id = 1").fetchone()
    if row:
        context = json.loads(row["payload"])
        if {"max_workers", "interval"} <= context.get("runtime", {}).keys() and "workers" in context.get("file_defaults", {}):
            return context
    return None


def _snapshot(conn) -> SettingsSnapshot:
    raw = _read_settings(conn)
    raw["dispatch"] = {**DispatchOverrides().model_dump(), **raw["dispatch"]}
    context = _context(conn)
    errors = []
    try:
        settings = Settings.model_validate(raw)
        validate_dispatch_overrides(settings.dispatch, context)
        validate_heartbeat_timeouts(settings.intent_timeout, settings.reason_timeout, context)
    except ValidationError as exc:
        errors = validation_messages(exc)
    except ValueError as exc:
        errors = [str(exc)]
    return SettingsSnapshot(**raw, validation_errors=errors, limits=settings_limits(context),
                            file_revision=context.get("file_revision") if context else None)


@router.get("/settings", response_model=SettingsSnapshot)
def get_settings():
    # Saved controls and the file revision must come from the same WAL snapshot.
    with get_read_conn() as conn:
        return _snapshot(conn)


@router.put("/settings", response_model=SettingsSnapshot)
def update_settings(body: Settings):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Older clients only submit heartbeat leases. Preserve live overrides
        # unless the caller explicitly replaces them ({} clears all overrides).
        raw = body.model_dump()
        if "dispatch" not in body.model_fields_set:
            raw["dispatch"] = _read_settings(conn)["dispatch"]
        context = _context(conn)
        if "file_revision" in body.model_fields_set and body.file_revision != (context.get("file_revision") if context else None):
            raise HTTPException(409, detail="dispatch.yaml changed while Settings was open. Reload settings before saving.")
        if context and context.get("file_revision") and "dispatch" in body.model_fields_set and body.dispatch != DispatchOverrides() and "file_revision" not in body.model_fields_set:
            raise HTTPException(409, detail="Reload settings and include the current file_revision before saving live changes.")
        try:
            settings = Settings.model_validate(raw)
            if context and (settings.dispatch.routing or settings.dispatch.worker_max_running):
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(context["reported_at"])).total_seconds()
                if age > 90:
                    raise ValueError("The dispatcher report is stale; wait for a fresh report before changing workers")
            validate_dispatch_overrides(settings.dispatch, context)
            validate_heartbeat_timeouts(settings.intent_timeout, settings.reason_timeout, context)
        except ValidationError as exc:
            raise HTTPException(422, detail="; ".join(validation_messages(exc))) from exc
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        conn.execute(
            "UPDATE settings SET intent_timeout = ?, reason_timeout = ?, dispatch = ? WHERE rowid = 1",
            (settings.intent_timeout, settings.reason_timeout, settings.dispatch.model_dump_json()),
        )
        return _snapshot(conn)


class EffectiveConfig(BaseModel):
    runtime: dict
    tasks: dict
    workers: list[dict]
    routing: dict[str, list[str]]
    file_defaults: dict
    applied_overrides: DispatchOverrides
    heartbeat_interval: int | None = None
    error: str | None = None
    file_revision: str | None = Field(default=None, min_length=1, max_length=128)


@router.put("/settings/effective")
def report_effective(body: EffectiveConfig):
    payload = {**body.model_dump(), "reported_at": datetime.now(timezone.utc).isoformat()}
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = _context(conn)
        if body.file_revision and body.file_revision != (previous.get("file_revision") if previous else None):
            # An accepted disk edit is a new operator instruction. Old UI
            # overrides belong to the previous file and must not shadow a restore.
            conn.execute("UPDATE settings SET dispatch = '{}' WHERE rowid = 1")
        conn.execute("INSERT OR REPLACE INTO dispatch_effective VALUES (1, ?)", (json.dumps(payload),))
    return payload


@router.get("/settings/effective")
def get_effective():
    with get_conn() as conn:
        row = conn.execute("SELECT payload FROM dispatch_effective WHERE id = 1").fetchone()
    return json.loads(row["payload"]) if row else None
