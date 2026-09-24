from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from board.reporting.data import load_evidence
from board.reporting.evidence import hash_file, infer_evidence_kind
from board.reporting.paths import default_projects_root, project_dir
from board.server.db import get_conn
from board.server.models import Evidence, RegisterEvidenceRequest
from board.server.services import get_project_or_404, next_evidence_id, utcnow

router = APIRouter(tags=["evidence"])


@router.get("/projects/{project_id}/evidence", response_model=list[Evidence])
def list_evidence(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return [_row_to_model(row) for row in load_evidence(conn, project_id)]


@router.post("/projects/{project_id}/evidence", response_model=Evidence)
def register_evidence(project_id: str, body: RegisterEvidenceRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)

        path = _normalize_path(project_id, body.path)
        sha256 = body.sha256
        size_bytes = body.size_bytes

        if sha256 is None or size_bytes is None:
            resolved = _resolve_file(project_id, body.path)
            if resolved is not None:
                computed_sha, computed_size = hash_file(resolved)
                sha256 = sha256 or computed_sha
                size_bytes = size_bytes if size_bytes is not None else computed_size
            elif sha256 is None and body.kind != "lost":
                raise HTTPException(
                    400,
                    "evidence file not found under the projects root and no sha256 provided",
                )

        duplicate = conn.execute(
            "SELECT * FROM evidence WHERE project_id = ? AND path = ? AND sha256 IS ?",
            (project_id, path, sha256),
        ).fetchone()
        if duplicate is not None:
            return _row_to_model(duplicate)

        now = utcnow()
        evid = next_evidence_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO evidence (id, project_id, intent_id, fact_id, path, kind, sha256, size_bytes, created_at, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evid,
                project_id,
                body.intent_id,
                body.fact_id,
                path,
                body.kind if body.kind is not None else infer_evidence_kind(body.path),
                sha256,
                size_bytes,
                now,
                body.description,
            ),
        )
        row = conn.execute(
            "SELECT * FROM evidence WHERE id = ? AND project_id = ?",
            (evid, project_id),
        ).fetchone()
        assert row is not None
        return _row_to_model(row)


def _row_to_model(row) -> Evidence:
    return Evidence(
        id=row["id"],
        intent_id=row["intent_id"],
        fact_id=row["fact_id"],
        path=row["path"],
        kind=row["kind"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
        description=row["description"],
    )


def _normalize_path(project_id: str, path: str) -> str:
    """Store paths relative to the projects root when they live under it."""
    candidate = Path(path)
    if not candidate.is_absolute():
        return str(candidate.as_posix()).lstrip("./")
    root = default_projects_root()
    try:
        relative = candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return str(candidate.as_posix())
    del project_id
    return relative.as_posix()


def _resolve_file(project_id: str, path: str) -> Path | None:
    """Locate the evidence file on this server, if it shares the projects root."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    under_project = project_dir(default_projects_root(), project_id) / "evidence" / candidate
    if under_project.is_file():
        return under_project
    under_root = default_projects_root() / candidate
    if under_root.is_file():
        return under_root
    return None
