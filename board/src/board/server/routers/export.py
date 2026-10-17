from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from board.reporting.agent_report import build_agent_report, render_agent_report_json, render_agent_report_markdown
from board.reporting.human_report import render_human_report
from board.reporting.paths import default_projects_root
from board.reporting.timeline import render_timeline
from board.reporting.yaml_export import render_graph_yaml
from board.server import db
from board.server.db import get_conn, get_read_conn
from board.server.services import expire_reason_leases, expire_workers, get_project_or_404

router = APIRouter(tags=["export"])


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline"):
        raise HTTPException(400, "Supported formats: yaml, timeline")

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)

    with get_read_conn() as conn:
        try:
            if format == "timeline":
                text = render_timeline(conn, project_id)
            else:
                text = render_graph_yaml(conn, project_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

        return Response(content=text, media_type="text/plain")


@router.get("/projects/{project_id}/report")
def project_report(project_id: str, type: str = "human"):
    if type not in ("human", "agent", "agent-json"):
        raise HTTPException(400, "Supported report types: human, agent, agent-json")

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)

    with get_read_conn() as conn:
        get_project_or_404(conn, project_id)

        evidence_root = default_projects_root()
        db_sha256 = db.hash_db_file()
        if type == "human":
            text = render_human_report(conn, project_id, evidence_root=evidence_root, db_sha256=db_sha256)
            media_type = "text/markdown"
        else:
            report = build_agent_report(
                conn, project_id, evidence_root=evidence_root, db_sha256=db_sha256
            )
            if type == "agent-json":
                text = render_agent_report_json(report)
                media_type = "application/json"
            else:
                text = render_agent_report_markdown(report)
                media_type = "text/markdown"

        return Response(content=text, media_type=media_type)
