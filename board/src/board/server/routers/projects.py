from uuid import uuid4

from fastapi import APIRouter, HTTPException

from board.server.db import get_conn, get_read_conn
from board.server.models import (
    AbortRequest,
    CompleteRequest,
    CreateProjectRequest,
    Fact,
    Hint,
    HeartbeatRequest,
    Intent,
    ProjectDetail,
    ProjectMeta,
    ProjectSummary,
    ReopenRequest,
    ReopenResponse,
    ReasonClaimRequest,
    ReasonWaitRequest,
    ReasonHeartbeatRequest,
    PlannerWait,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
    SuspensionAckRequest,
)
from board.planning import planner_input_signature
from board.server.services import (
    abort_project,
    build_intents,
    build_jobs,
    check_project_completed,
    check_project_active,
    clear_project_reason,
    expire_reason_leases,
    expire_workers,
    get_completion_intent_or_409,
    get_project_or_404,
    intent_to_model,
    next_fact_id,
    next_hint_id,
    next_intent_id,
    next_project_id,
    project_meta_from_row,
    project_reason_from_row,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects():
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expire_workers(conn)
        expire_reason_leases(conn)

    with get_read_conn() as conn:
        rows = conn.execute("""
            SELECT p.*,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND excluded = 0 AND worker IS NOT NULL) AS working_intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND excluded = 0 AND worker IS NULL AND dispatch_error IS NULL AND cancel_requested_at IS NULL) AS unclaimed_intent_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
            FROM projects p
            ORDER BY p.created_at
        """).fetchall()
        return [
            ProjectSummary(
                **project_meta_from_row(row).model_dump(),
                fact_count=row["fact_count"],
                intent_count=row["intent_count"],
                working_intent_count=row["working_intent_count"],
                unclaimed_intent_count=row["unclaimed_intent_count"],
                hint_count=row["hint_count"],
            )
            for row in rows
        ]


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        pid = next_project_id(conn)
        now = utcnow()

        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) VALUES (?, ?, 'active', ?, ?)",
            (pid, body.title, body.bootstrap_enabled, now),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("origin", pid, body.origin),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("goal", pid, body.goal),
        )

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        return ProjectDetail(
            project=ProjectMeta(
                id=pid,
                title=body.title,
                status="active",
                bootstrap_enabled=body.bootstrap_enabled,
                created_at=now,
                reason=None,
            ),
            facts=[
                Fact(id="origin", description=body.origin),
                Fact(id="goal", description=body.goal),
            ],
            intents=[],
            hints=hints,
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)

    with get_read_conn() as conn:
        row = get_project_or_404(conn, project_id)

        facts = conn.execute(
            "SELECT * FROM facts WHERE project_id = ?", (project_id,)
        ).fetchall()
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        return ProjectDetail(
            project=project_meta_from_row(row),
            facts=[Fact(**dict(f)) for f in facts],
            intents=build_intents(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
            jobs=build_jobs(conn, project_id),
        )


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status in ("completed", "aborted"):
            raise HTTPException(409, f"{current_status.capitalize()} projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(row)

        if body.status == "stopped":
            conn.execute(
                "UPDATE projects SET status = 'stopped', suspension_id = ?, suspension_ack_id = NULL WHERE id = ?",
                (uuid4().hex, project_id),
            )
            conn.execute(
                "UPDATE intents SET worker = NULL, last_heartbeat_at = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project_id,),
            )
            clear_project_reason(conn, project_id)
            # Detached jobs cannot be resumed as processes. End them, preserve their
            # output and request cleanup; subsequent work must be explicitly planned.
            conn.execute(
                """UPDATE jobs SET status = 'killed', finished_at = ?,
                   stop_state = CASE WHEN trim(coalesce(stop_command, '')) != '' THEN 'pending' ELSE 'manual' END,
                   stop_error = NULL WHERE project_id = ? AND status = 'running'""",
                (utcnow(), project_id),
            )
            message = "Project suspended by operator. No automatic resume. Background jobs requested to stop."
        else:
            if row["suspension_id"] and row["suspension_ack_id"] != row["suspension_id"]:
                raise HTTPException(409, "Suspending: waiting for the dispatcher to finish stopping workers")
            for jid in set(body.confirmed_stopped_job_ids):
                job = conn.execute("SELECT * FROM jobs WHERE project_id = ? AND id = ?", (project_id, jid)).fetchone()
                if job is None or job["stop_state"] not in ("manual", "failed", "stopped"):
                    raise HTTPException(409, f"Job {jid} cannot be confirmed stopped")
                conn.execute("UPDATE jobs SET stop_state = 'stopped', stop_error = NULL WHERE project_id = ? AND id = ?", (project_id, jid))
            unresolved = conn.execute(
                "SELECT id FROM jobs WHERE project_id = ? AND stop_state IN ('pending', 'manual', 'failed')", (project_id,)
            ).fetchall()
            if unresolved:
                raise HTTPException(409, "Background jobs still need to stop: " + ", ".join(j["id"] for j in unresolved))
            if body.excluded_intent_ids is not None:
                ids = set(body.excluded_intent_ids)
                open_ids = {i["id"] for i in conn.execute("SELECT id FROM intents WHERE project_id = ? AND to_fact_id IS NULL", (project_id,))}
                if not ids <= open_ids:
                    raise HTTPException(409, "Only unfinished intents can be excluded; refresh the project")
                conn.execute("UPDATE intents SET excluded = 0 WHERE project_id = ? AND to_fact_id IS NULL", (project_id,))
                conn.executemany("UPDATE intents SET excluded = 1 WHERE project_id = ? AND id = ?", [(project_id, iid) for iid in ids])
            notes = row["execution_notes"] if body.execution_notes is None else body.execution_notes.strip()
            conn.execute("UPDATE projects SET status = 'active', execution_notes = ? WHERE id = ?", (notes, project_id))
            excluded = [i["id"] for i in conn.execute("SELECT id FROM intents WHERE project_id = ? AND excluded = 1 ORDER BY id", (project_id,))]
            message = f"Project resumed by operator. Excluded intents: {', '.join(excluded) or 'none'}."
            if notes:
                message += f" Current execution instructions: {notes}"
        conn.execute("INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, 'human', ?)",
                     (next_hint_id(conn, project_id), project_id, message, utcnow()))
        updated = get_project_or_404(conn, project_id)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/suspension/ack", response_model=ProjectMeta)
def acknowledge_suspension(project_id: str, body: SuspensionAckRequest):
    """Dispatcher acknowledges that the old worker processes have exited."""
    with get_conn() as conn:
        row = get_project_or_404(conn, project_id)
        if row["status"] != "stopped" or row["suspension_id"] != body.suspension_id:
            raise HTTPException(409, "Suspension changed")
        conn.execute("UPDATE projects SET suspension_ack_id = ? WHERE id = ?", (body.suspension_id, project_id))
        return project_meta_from_row(get_project_or_404(conn, project_id))


@router.post("/projects/{project_id}/abort", response_model=ProjectMeta)
def abort_project_endpoint(project_id: str, body: AbortRequest):
    """Terminate a project as abandoned (not goal-met) with a recorded reason.

    Terminal like ``completed`` but honest about the outcome: the reason lands in
    the timeline, the reports and the project record. The dispatcher cancels
    running tasks on its next poll and writes the full report set.
    """
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        updated = abort_project(conn, project_id, body.reason, body.actor)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta)
def claim_project_reason(project_id: str, body: ReasonClaimRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is not None and current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if current_worker == body.worker:
            return project_meta_from_row(row)

        now = utcnow()
        conn.execute(
            """
            UPDATE projects
            SET reason_worker = ?,
                reason_trigger = ?,
                reason_started_at = ?,
                reason_last_heartbeat_at = ?,
                reason_lease_id = ?,
                planner_wait = NULL
            WHERE id = ?
            """,
            (body.worker, body.trigger, now, now, uuid4().hex, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/wait", response_model=ProjectMeta)
def record_reason_wait(project_id: str, body: ReasonWaitRequest):
    with get_conn() as conn:
        # Serialize the lease/input check with the write, including other writers.
        conn.execute("BEGIN IMMEDIATE")
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        if row["reason_worker"] != body.worker or row["reason_lease_id"] != body.lease_id:
            raise HTTPException(409, "Planner lease changed; discarded stale wait decision")
        current = ProjectDetail(
            project=project_meta_from_row(row),
            facts=[Fact(**dict(f)) for f in conn.execute("SELECT * FROM facts WHERE project_id = ?", (project_id,))],
            hints=[Hint(**dict(h)) for h in conn.execute("SELECT * FROM hints WHERE project_id = ? ORDER BY created_at", (project_id,))],
            intents=build_intents(conn, project_id), jobs=build_jobs(conn, project_id),
        )
        if planner_input_signature(current) != body.input_signature:
            raise HTTPException(409, "Planner inputs changed; discarded stale wait decision")
        decision = PlannerWait(reason=body.reason, since=utcnow(), worker=body.worker,
                               input_signature=body.input_signature)
        conn.execute("UPDATE projects SET planner_wait = ? WHERE id = ?", (decision.model_dump_json(), project_id))
        return project_meta_from_row(get_project_or_404(conn, project_id))


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta)
def heartbeat_project_reason(project_id: str, body: ReasonHeartbeatRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project reason is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if body.lease_id is not None and row["reason_lease_id"] != body.lease_id:
            raise HTTPException(409, "Planner lease changed")

        now = utcnow()
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = ? WHERE id = ?",
            (now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta)
def release_project_reason(project_id: str, body: ReasonHeartbeatRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            return project_meta_from_row(row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if body.lease_id is not None and row["reason_lease_id"] != body.lease_id:
            raise HTTPException(409, "Planner lease changed")

        clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/complete", response_model=Intent)
def complete_project(project_id: str, body: CompleteRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        if body.outcome == "current_results":
            # A bounded conclusion must not silently discard active work.
            if conn.execute("SELECT 1 FROM intents WHERE project_id = ? AND concluded_at IS NULL AND excluded = 0 LIMIT 1", (project_id,)).fetchone():
                raise HTTPException(409, "Conclude or exclude unfinished intents before closing with current results")
            if conn.execute("SELECT 1 FROM jobs WHERE project_id = ? AND status = 'running' LIMIT 1", (project_id,)).fetchone():
                raise HTTPException(409, "Stop running jobs before closing with current results")
            if conn.execute("SELECT 1 FROM jobs WHERE project_id = ? AND stop_state IN ('pending', 'manual', 'failed') LIMIT 1", (project_id,)).fetchone():
                raise HTTPException(409, "Confirm background jobs have stopped before closing with current results")
            if get_project_or_404(conn, project_id)["reason_worker"] is not None:
                raise HTTPException(409, "Wait for the planner to finish before closing with current results")
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)

        now = utcnow()
        iid = next_intent_id(conn, project_id)

        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, 'goal', ?, ?, ?, ?, ?, ?)",
            (iid, project_id, body.description, body.worker, body.worker, now, now, now),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )
        conn.execute(
            """
            UPDATE projects
            SET status = 'completed',
                completion_outcome = ?,
                reason_worker = NULL,
                reason_trigger = NULL,
                reason_started_at = NULL,
                reason_lease_id = NULL,
                reason_last_heartbeat_at = NULL
            WHERE id = ?
            """,
            (body.outcome, project_id),
        )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to="goal",
            description=body.description,
            creator=body.worker,
            worker=body.worker,
            last_heartbeat_at=now,
            created_at=now,
            concluded_at=now,
        )


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_intent_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion intent is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        description = body.description
        creator = body.creator

        conn.execute(
            "DELETE FROM intents WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, description),
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (intent_id, project_id, fact_id, "external_feedback", creator, creator, now, now, now),
        )
        for source_id in source_ids:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (intent_id, project_id, source_id),
            )
        clear_project_reason(conn, project_id)
        conn.execute(
            "UPDATE projects SET status = 'active', completion_outcome = NULL WHERE id = ?",
            (project_id,),
        )

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_intent = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_intent is not None
        return ReopenResponse(
            project=project_meta_from_row(updated_project),
            fact=Fact(id=fact_id, description=description),
            intent=intent_to_model(conn, updated_intent, project_id),
        )
