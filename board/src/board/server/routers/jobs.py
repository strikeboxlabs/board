from fastapi import APIRouter, HTTPException

from board.server.db import get_conn
from board.server.models import (
    CreateJobRequest,
    Fact,
    FinishJobRequest,
    Job,
    JobProgressRequest,
    KillJobRequest,
    JobStopResultRequest,
)
from board.server.services import (
    check_project_active,
    build_jobs,
    get_project_or_404,
    next_fact_id,
    next_job_id,
    utcnow,
)

router = APIRouter(tags=["jobs"])

JOB_CREATOR_WORKER = "dispatcher.jobs"


def _row_to_job(row, project_id: str) -> Job:
    return Job(
        id=row["id"],
        project_id=project_id,
        description=row["description"],
        status=row["status"],
        check_command=row["check_command"],
        stop_command=row["stop_command"],
        stop_state=row["stop_state"],
        stop_error=row["stop_error"],
        budget_seconds=row["budget_seconds"],
        creator=row["creator"],
        last_check_at=row["last_check_at"],
        last_output=row["last_output"],
        fact_id=row["fact_id"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


def _get_running_job_or_409(conn, project_id: str, job_id: str):
    row = conn.execute(
        "SELECT * FROM jobs WHERE id = ? AND project_id = ?",
        (job_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Job not found")
    if row["status"] != "running":
        raise HTTPException(409, f"Job is already {row['status']}")
    return row


@router.get("/projects/{project_id}/jobs", response_model=list[Job])
def list_jobs(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_jobs(conn, project_id)


@router.post("/projects/{project_id}/jobs", response_model=Job, status_code=201)
def create_job(project_id: str, body: CreateJobRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)

        now = utcnow()
        jid = next_job_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO jobs (id, project_id, description, status, check_command, stop_command, budget_seconds, creator, created_at)
            VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
            """,
            (jid, project_id, body.description, body.check_command, body.stop_command, body.budget_seconds, body.creator, now),
        )
        return Job(
            id=jid,
            project_id=project_id,
            description=body.description,
            status="running",
            check_command=body.check_command,
            stop_command=body.stop_command,
            budget_seconds=body.budget_seconds,
            creator=body.creator,
            created_at=now,
        )


@router.post("/projects/{project_id}/jobs/{job_id}/progress", response_model=Job)
def progress_job(project_id: str, job_id: str, body: JobProgressRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        _get_running_job_or_409(conn, project_id, job_id)

        now = utcnow()
        conn.execute(
            "UPDATE jobs SET last_check_at = ?, last_output = ? WHERE id = ? AND project_id = ?",
            (now, body.output[:2000], job_id, project_id),
        )
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND project_id = ?",
            (job_id, project_id),
        ).fetchone()
        return _row_to_job(row, project_id)


@router.post("/projects/{project_id}/jobs/{job_id}/finish")
def finish_job(project_id: str, job_id: str, body: FinishJobRequest):
    """Move a running job to a terminal status and write its outcome as a fact.

    The synthetic fact is the only side effect reason needs: it lands in the
    graph like any worker-discovered fact and triggers a reason pass through
    the normal facts-count checkpoint.
    """
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = _get_running_job_or_409(conn, project_id, job_id)

        now = utcnow()
        result = body.result.strip()[:4000] or "no output"
        description = f"[job {job_id} · {body.status}] {row['description']} — {result}"

        fact_id = next_fact_id(conn, project_id)
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, description),
        )
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, finished_at = ?, fact_id = ?, last_check_at = ?, last_output = ?
            WHERE id = ? AND project_id = ?
            """,
            (body.status, now, fact_id, now, result, job_id, project_id),
        )
        terminal = conn.execute("SELECT status, stop_command FROM jobs WHERE project_id = ? AND id = ?", (project_id, job_id)).fetchone()
        if terminal["status"] in ("killed", "exhausted"):
            state = "pending" if (terminal["stop_command"] or "").strip() else "manual"
            conn.execute("UPDATE jobs SET stop_state = ? WHERE project_id = ? AND id = ?", (state, project_id, job_id))
        return {
            "job": _row_to_job(conn.execute(
                "SELECT * FROM jobs WHERE id = ? AND project_id = ?",
                (job_id, project_id),
            ).fetchone(), project_id),
            "fact": Fact(id=fact_id, description=description),
        }


@router.post("/projects/{project_id}/jobs/{job_id}/kill")
def kill_job(project_id: str, job_id: str, body: KillJobRequest):
    """Human override: terminate a running job.

    Transitions the job to ``killed`` and writes the outcome as a fact (like
    any other terminal transition). The dispatcher notices the status change
    on its next poll and executes the job's stop_command, if one was
    registered, to actually end the underlying process.
    """
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = _get_running_job_or_409(conn, project_id, job_id)

        now = utcnow()
        actor = body.actor or body.worker
        result = (body.reason or "").strip()[:2000] or "killed by human operator"
        description = f"[job {job_id} · killed] {row['description']} — {result} (by {actor})"

        fact_id = next_fact_id(conn, project_id)
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, description),
        )
        conn.execute(
            """
            UPDATE jobs
            SET status = 'killed', finished_at = ?, fact_id = ?, last_check_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (now, fact_id, now, job_id, project_id),
        )
        terminal = conn.execute("SELECT status, stop_command FROM jobs WHERE project_id = ? AND id = ?", (project_id, job_id)).fetchone()
        if terminal["status"] in ("killed", "exhausted"):
            state = "pending" if (terminal["stop_command"] or "").strip() else "manual"
            conn.execute("UPDATE jobs SET stop_state = ? WHERE project_id = ? AND id = ?", (state, project_id, job_id))
        return {
            "job": _row_to_job(conn.execute(
                "SELECT * FROM jobs WHERE id = ? AND project_id = ?",
                (job_id, project_id),
            ).fetchone(), project_id),
            "fact": Fact(id=fact_id, description=description),
        }


@router.post("/projects/{project_id}/jobs/{job_id}/stop-result", response_model=Job)
def record_job_stop(project_id: str, job_id: str, body: JobStopResultRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute("SELECT * FROM jobs WHERE project_id = ? AND id = ?", (project_id, job_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Job not found")
        if row["stop_state"] == "stopped" and body.success:
            return _row_to_job(row, project_id)
        if row["stop_state"] != "pending":
            raise HTTPException(409, "No pending stop request")
        conn.execute("UPDATE jobs SET stop_state = ?, stop_error = ? WHERE project_id = ? AND id = ?",
                     ("stopped" if body.success else "failed", None if body.success else body.error or "Stop command failed", project_id, job_id))
        return _row_to_job(conn.execute("SELECT * FROM jobs WHERE project_id = ? AND id = ?", (project_id, job_id)).fetchone(), project_id)
