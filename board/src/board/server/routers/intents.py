from fastapi import APIRouter, HTTPException, Response

from board.server.db import get_conn
from board.server.models import (
    ConcludeRequest,
    ConcludeResponse,
    CancelIntentRequest,
    FailIntentRequest,
    CreateIntentRequest,
    Fact,
    HeartbeatRequest,
    Intent,
    UpdateIntentAssignmentRequest,
    UpdateIntentPriorityRequest,
)
from board.server.services import (
    check_project_active,
    get_claimable_open_intent_or_404,
    get_releasable_open_intent_or_404,
    get_intent_or_404,
    get_project_or_404,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["intents"])


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(body.creator, body.worker)

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        claimed = body.worker is not None
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at, priority, blocked_by, assigned_worker, assigned_worker_type) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.worker,
                now if claimed else None,
                now,
                body.priority,
                body.blocked_by,
                body.assigned_worker,
                body.assigned_worker_type,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            creator=body.creator,
            worker=body.worker,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
            concluded_at=None,
            priority=body.priority,
            blocked_by=body.blocked_by,
            assigned_worker=body.assigned_worker,
            assigned_worker_type=body.assigned_worker_type,
        )


@router.put(
    "/projects/{project_id}/intents/{intent_id}/priority",
    response_model=Intent,
)
def update_intent_priority(project_id: str, intent_id: str, body: UpdateIntentPriorityRequest):
    """Human override of the strategist's dispatch ordering.

    Takes effect on the dispatcher's next tick for intents not yet claimed;
    running and concluded intents keep their behavior (concluded ones are
    rejected outright).
    """
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Concluded intents cannot change priority")
        now = utcnow()
        conn.execute(
            "UPDATE intents SET priority = ?, priority_set_by = ?, priority_set_at = ? WHERE id = ? AND project_id = ?",
            (body.priority, body.actor, now, intent_id, project_id),
        )
        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.put(
    "/projects/{project_id}/intents/{intent_id}/assign",
    response_model=Intent,
)
def update_intent_assignment(project_id: str, intent_id: str, body: UpdateIntentAssignmentRequest):
    """Route an intent to a worker name and/or type (clear with null).

    Both constraints apply when provided. Unavailable matching workers cause
    the intent to wait while other runnable intents dispatch around it.
    """
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_intent_or_404(conn, project_id, intent_id)
        conn.execute(
            "UPDATE intents SET assigned_worker = ? WHERE id = ? AND project_id = ?",
            (body.assigned_worker, intent_id, project_id),
        )
        if "assigned_worker_type" in body.model_fields_set:
            conn.execute(
                "UPDATE intents SET assigned_worker_type = ? WHERE id = ? AND project_id = ?",
                (body.assigned_worker_type, intent_id, project_id),
            )
        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        conn.execute(
            "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE id = ? AND project_id = ?",
            (body.worker, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        if row["worker"] == body.worker:
            conn.execute(
                """UPDATE intents SET worker = NULL,
                   cancelled_at = CASE WHEN cancel_requested_at IS NOT NULL THEN COALESCE(cancelled_at, ?) ELSE cancelled_at END
                   WHERE id = ? AND project_id = ?""",
                (utcnow(), intent_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            ).fetchone()

        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        fid = next_fact_id(conn, project_id)

        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fid, project_id, body.description),
        )
        conn.execute(
            "UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ?",
            (fid, body.worker, now, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()

        return ConcludeResponse(
            fact=Fact(id=fid, description=body.description),
            intent=intent_to_model(conn, updated, project_id),
        )


@router.post("/projects/{project_id}/intents/{intent_id}/fail", response_model=Intent)
def fail_intent(project_id: str, intent_id: str, body: FailIntentRequest):
    """Record an ended attempt, release its claim, and prevent automatic retries."""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        get_project_or_404(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Concluded intents cannot fail")
        if row["worker"] is not None and row["worker"] != body.worker:
            raise HTTPException(409, "Intent belongs to another worker")
        now = utcnow()
        if row["cancel_requested_at"]:
            conn.execute("UPDATE intents SET worker = NULL, cancelled_at = ? WHERE id = ? AND project_id = ?",
                         (now, intent_id, project_id))
        else:
            conn.execute("""UPDATE intents SET worker = NULL, dispatch_error = ?, dispatch_error_at = ?,
                            dispatch_error_worker = ? WHERE id = ? AND project_id = ?""",
                         (body.error, now, body.worker, intent_id, project_id))
        return intent_to_model(conn, get_intent_or_404(conn, project_id, intent_id), project_id)


@router.post("/projects/{project_id}/intents/{intent_id}/cancel", response_model=Intent)
def cancel_intent(project_id: str, intent_id: str, body: CancelIntentRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        get_project_or_404(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Concluded intents are part of the project history")
        if not row["cancel_requested_at"]:
            now = utcnow()
            conn.execute("""UPDATE intents SET cancel_requested_at = ?, cancelled_by = ?, cancelled_at = ?
                            WHERE id = ? AND project_id = ?""",
                         (now, body.actor.strip(), now if row["worker"] is None else None, intent_id, project_id))
        return intent_to_model(conn, get_intent_or_404(conn, project_id, intent_id), project_id)


@router.post("/projects/{project_id}/intents/{intent_id}/cancelled", response_model=Intent)
def acknowledge_intent_cancel(project_id: str, intent_id: str, body: HeartbeatRequest):
    """Dispatcher acknowledgement, only after the managed process has ended."""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = get_intent_or_404(conn, project_id, intent_id)
        if not row["cancel_requested_at"]:
            raise HTTPException(409, "Cancellation has not been requested")
        if row["worker"] is not None and row["worker"] != body.worker:
            raise HTTPException(409, "Intent belongs to another worker")
        conn.execute("UPDATE intents SET worker = NULL, cancelled_at = COALESCE(cancelled_at, ?) WHERE id = ? AND project_id = ?",
                     (utcnow(), intent_id, project_id))
        return intent_to_model(conn, get_intent_or_404(conn, project_id, intent_id), project_id)


@router.delete("/projects/{project_id}/intents/{intent_id}", status_code=204)
def delete_intent(project_id: str, intent_id: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        get_project_or_404(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Concluded intents are part of the project history")
        if row["worker"] is not None or not row["cancelled_at"]:
            raise HTTPException(409, "Cancel the intent and wait for the worker to stop before deleting it")
        suffix = ":" + intent_id
        active_job = conn.execute("""SELECT id FROM jobs WHERE project_id = ? AND substr(creator, -?) = ?
            AND (status = 'running' OR stop_state IN ('pending', 'failed', 'manual')) LIMIT 1""",
            (project_id, len(suffix), suffix)).fetchone()
        if active_job:
            raise HTTPException(409, "An associated background job still needs to be stopped")
        conn.execute("UPDATE evidence SET intent_id = NULL WHERE project_id = ? AND intent_id = ?", (project_id, intent_id))
        conn.execute("UPDATE findings SET intent_id = NULL WHERE project_id = ? AND intent_id = ?", (project_id, intent_id))
        conn.execute("DELETE FROM intents WHERE project_id = ? AND id = ?", (project_id, intent_id))
    return Response(status_code=204)
