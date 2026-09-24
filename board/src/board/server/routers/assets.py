from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from board.reporting.asset_map_html import render_asset_map_html
from board.reporting.assets import build_asset_map, render_asset_inventory_md, render_asset_map_json
from board.server.db import get_conn
from board.server.services import get_project_or_404

router = APIRouter(tags=["assets"])


@router.get("/projects/{project_id}/asset-map")
def project_asset_map(project_id: str, format: str = "html"):
    """Attack-surface asset map rendered from facts + findings. Works on any status."""
    if format not in ("html", "json", "md"):
        raise HTTPException(400, "Supported formats: html, json, md")

    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        asset_map = build_asset_map(conn, project_id)

    if format == "json":
        return Response(content=render_asset_map_json(asset_map), media_type="application/json")
    if format == "md":
        return Response(content=render_asset_inventory_md(asset_map), media_type="text/markdown")
    return Response(content=render_asset_map_html(asset_map), media_type="text/html")
