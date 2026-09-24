"""Blackboard data loading for renderers.

Deliberately free of FastAPI/HTTP concerns so the renderers can be used
from the server, the dispatcher and the CLI alike. Raises ``LookupError``
for unknown projects; API routes translate that to 404.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class ProjectData:
    project: sqlite3.Row
    facts: list[sqlite3.Row]
    hints: list[sqlite3.Row]
    intents: list[sqlite3.Row]
    sources_by_intent: dict[str, list[str]] = field(default_factory=dict)
    jobs: list[sqlite3.Row] = field(default_factory=list)


def _load_jobs(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def load_project_data(conn: sqlite3.Connection, project_id: str) -> ProjectData:
    proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if proj is None:
        raise LookupError(f"project not found: {project_id}")

    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    hints = conn.execute(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    jobs = _load_jobs(conn, project_id)

    sources_by_intent: dict[str, list[str]] = {}
    for i in intents:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (i["id"], project_id),
        ).fetchall()
        sources_by_intent[i["id"]] = [r["fact_id"] for r in rows]

    return ProjectData(
        project=proj,
        facts=facts,
        hints=hints,
        intents=intents,
        sources_by_intent=sources_by_intent,
        jobs=jobs,
    )


def load_evidence(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM evidence WHERE project_id = ? ORDER BY created_at, id",
        (project_id,),
    ).fetchall()


def load_findings(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM findings WHERE project_id = ? ORDER BY created_at, id",
        (project_id,),
    ).fetchall()


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
