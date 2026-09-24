"""Abort flow: terminal 'aborted' status, reason recording, reports on abandon."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from board.cli import main as cli_main
from board.server import db
from board.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "board.db")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def projects_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "projects"
    monkeypatch.setenv("BOARD_PROJECTS_DIR", str(root))
    return root


def _seed_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={"title": "going nowhere", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _claimed_intent(client: TestClient, project_id: str) -> str:
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "explorer", "worker": "explorer"},
    )
    assert response.status_code == 201
    return response.json()["id"]


# --------------------------------------------------------------------------- server API


def test_abort_is_terminal_and_releases_claims(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    intent_id = _claimed_intent(client, project_id)

    response = client.post(
        f"/projects/{project_id}/abort",
        json={"reason": "foreign-tenant account unobtainable", "actor": "F"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "aborted"
    assert body["abort_reason"] == "foreign-tenant account unobtainable"
    assert body["aborted_by"] == "F"
    assert body["aborted_at"]

    # claims released: the open intent is unclaimed again
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None

    # terminal: no re-abort, no status change, no complete, no reopen
    assert client.post(f"/projects/{project_id}/abort", json={"reason": "again"}).status_code == 409
    assert client.put(f"/projects/{project_id}/status", json={"status": "active"}).status_code == 409
    assert client.post(f"/projects/{project_id}/complete", json={"from": ["origin"], "description": "x", "worker": "w"}).status_code == 403
    assert client.post(f"/projects/{project_id}/reopen", json={"description": "x", "creator": "F"}).status_code == 403


def test_abort_rejects_completed_and_allows_stopped(client: TestClient, projects_root: Path) -> None:
    completed_id = _seed_project(client)
    client.post(
        f"/projects/{completed_id}/complete",
        json={"from": ["origin"], "description": "done", "worker": "w"},
    )
    assert client.post(f"/projects/{completed_id}/abort", json={"reason": "r"}).status_code == 409

    stopped_id = _seed_project(client)
    client.put(f"/projects/{stopped_id}/status", json={"status": "stopped"})
    response = client.post(f"/projects/{stopped_id}/abort", json={"reason": "gave up while paused"})
    assert response.status_code == 200
    assert response.json()["aborted_by"] == "human"  # actor optional


def test_abort_reason_is_required(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    assert client.post(f"/projects/{project_id}/abort", json={"reason": "  "}).status_code == 422


# --------------------------------------------------------------------------- renderers


def test_aborted_project_renders_across_timeline_and_reports(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    intent_id = _claimed_intent(client, project_id)
    client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude",
        json={"worker": "explorer", "description": "partial result only"},
    )
    client.post(f"/projects/{project_id}/abort", json={"reason": "scope cancelled by client", "actor": "F"})

    timeline = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert "PROJECT ABORTED by F" in timeline
    assert "scope cancelled by client" in timeline

    human = client.get(f"/projects/{project_id}/report?type=human").text
    assert "**Status:** aborted — scope cancelled by client" in human
    assert "**Outcome.**" in human
    assert "aborted before the goal was reached" in human
    assert "never executed" in human  # unexecuted intents section covers the declared-not-concluded path

    agent_json = client.get(f"/projects/{project_id}/report?type=agent-json").json()
    assert agent_json["header"]["status"] == "aborted"
    assert agent_json["header"]["abort_reason"] == "scope cancelled by client"
    assert agent_json["header"]["aborted_by"] == "F"


# --------------------------------------------------------------------------- migration


def test_configure_adds_abort_columns_to_legacy_projects_table(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                reason_worker TEXT,
                reason_trigger TEXT,
                reason_started_at TEXT,
                reason_last_heartbeat_at TEXT
            )
            """
        )
        conn.execute("INSERT INTO projects (id, title, created_at) VALUES ('proj_001', 'legacy', '2026-01-01T00:00:00Z')")

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        row = conn.execute("SELECT abort_reason, aborted_at, aborted_by FROM projects WHERE id = 'proj_001'").fetchone()
    assert row["abort_reason"] is None
    assert row["aborted_at"] is None
    assert row["aborted_by"] is None


# --------------------------------------------------------------------------- CLI


def test_cli_abort_writes_full_report_set(client: TestClient, tmp_path, monkeypatch) -> None:
    project_id = _seed_project(client)
    projects_root = tmp_path / "projects"
    monkeypatch.setenv("BOARD_PROJECTS_DIR", str(projects_root))
    monkeypatch.setattr(db, "_db_path", None)
    runner = CliRunner()

    result = runner.invoke(
        cli_main,
        [
            "abort", project_id,
            "--reason", "diminishing returns after i010",
            "--actor", "F",
            "--db-path", str(tmp_path / "board.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "aborted" in result.output

    projects_root = Path(projects_root)
    reports = projects_root / project_id / "reports"
    assert sorted(p.name for p in reports.iterdir()) == [
        "asset-inventory.md",
        "asset-map.html",
        "asset-map.json",
        "graph.yaml",
        "report-agent.json",
        "report-agent.md",
        "report-human.md",
        "timeline.txt",
    ]
    agent = json.loads((reports / "report-agent.json").read_text())
    assert agent["header"]["abort_reason"] == "diminishing returns after i010"
    assert "PROJECT ABORTED by F" in (reports / "timeline.txt").read_text()

    # terminal: second abort refuses cleanly
    again = runner.invoke(cli_main, ["abort", project_id, "--reason", "x", "--db-path", str(tmp_path / "board.db")])
    assert again.exit_code != 0
    assert "aborted" in again.output.lower()
