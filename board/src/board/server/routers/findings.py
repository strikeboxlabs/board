from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from board.reporting.data import load_findings
from board.server.db import get_conn
from board.server.models import (
    FINDING_SEVERITY_ORDER,
    CreateFindingRequest,
    Finding,
    UpdateFindingRequest,
    finding_row_to_model,
)
from board.server.services import (
    get_project_or_404,
    next_finding_id,
    utcnow,
    validate_facts_exist,
)

router = APIRouter(tags=["findings"])


@router.get("/projects/{project_id}/findings", response_model=list[Finding])
def list_findings(project_id: str, severity: str | None = None, status: str | None = None):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = [
            row
            for row in load_findings(conn, project_id)
            if (severity is None or row["severity"] == severity)
            and (status is None or row["status"] == status)
        ]
        rows.sort(key=_severity_sort_key)
        return [finding_row_to_model(row) for row in rows]


@router.post("/projects/{project_id}/findings", response_model=Finding, status_code=201)
def create_finding(project_id: str, body: CreateFindingRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        validate_facts_exist(conn, project_id, [body.fact_id])
        if body.intent_id is not None:
            intent = conn.execute(
                "SELECT 1 FROM intents WHERE id = ? AND project_id = ?",
                (body.intent_id, project_id),
            ).fetchone()
            if intent is None:
                raise HTTPException(404, f"Intent {body.intent_id} not found")

        now = utcnow()
        fid = next_finding_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO findings (
                id, project_id, fact_id, intent_id, title, category, severity, status,
                affected_target, description, recommendation, evidence_ids, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fid,
                project_id,
                body.fact_id,
                body.intent_id,
                body.title,
                body.category,
                body.severity,
                body.status,
                body.affected_target,
                body.description,
                body.recommendation,
                json.dumps(body.evidence_ids),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM findings WHERE id = ? AND project_id = ?",
            (fid, project_id),
        ).fetchone()
        assert row is not None
        return finding_row_to_model(row)


@router.get("/projects/{project_id}/findings/{finding_id}", response_model=Finding)
def get_finding(project_id: str, finding_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = _get_finding_or_404(conn, project_id, finding_id)
        return finding_row_to_model(row)


@router.put("/projects/{project_id}/findings/{finding_id}", response_model=Finding)
def update_finding(project_id: str, finding_id: str, body: UpdateFindingRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = _get_finding_or_404(conn, project_id, finding_id)

        updates = body.model_dump(exclude_unset=True, exclude_none=True)
        if "evidence_ids" in updates:
            updates["evidence_ids"] = json.dumps(updates["evidence_ids"])
        if updates:
            updates["updated_at"] = utcnow()
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE findings SET {assignments} WHERE id = ? AND project_id = ?",
                (*updates.values(), finding_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM findings WHERE id = ? AND project_id = ?",
                (finding_id, project_id),
            ).fetchone()
        assert row is not None
        return finding_row_to_model(row)


def _get_finding_or_404(conn, project_id: str, finding_id: str):
    row = conn.execute(
        "SELECT * FROM findings WHERE id = ? AND project_id = ?",
        (finding_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"Finding {finding_id} not found")
    return row


def _severity_sort_key(row) -> tuple[int, str, str]:
    severity = row["severity"] or "info"
    return (FINDING_SEVERITY_ORDER.get(severity, FINDING_SEVERITY_ORDER["info"]), row["created_at"], row["id"])
