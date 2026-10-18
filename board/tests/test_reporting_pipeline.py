"""Reporting & evidence pipeline tests: renderers, evidence store, findings, reports, CLI.

Everything here runs without Docker and without live model endpoints — the
renderers and DB layers must stay testable standalone (directive 5.3).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from board.cli import main as cli_main
from board.dispatcher.config import LocalConfig
from board.dispatcher.contracts import validate_explore_payload
from board.dispatcher.protocol.client import ApiResult
from board.dispatcher.runtime.local_backend import LocalBackend
from board.dispatcher.tasks.common import insert_worker_findings
from board.reporting.agent_report import build_agent_report, render_agent_report_json
from board.reporting.human_report import render_human_report
from board.reporting.paths import default_projects_root
from board.reporting.timeline import render_timeline
from board.reporting.yaml_export import render_graph_yaml
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
        json={
            "title": "elp external test",
            "origin": "elp.envo-it.dk from the internet",
            "goal": "assess external security posture",
            "hints": [{"content": "scope: web perimeter only", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _conclude_fact(client: TestClient, project_id: str, description: str, intent_from: str = "origin") -> str:
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": [intent_from], "description": "investigate", "creator": "reasoner"},
    )
    intent_id = response.json()["id"]
    client.post(f"/projects/{project_id}/intents/{intent_id}/heartbeat", json={"worker": "explorer"})
    conclude = client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude",
        json={"worker": "explorer", "description": description},
    )
    return conclude.json()["fact"]["id"]


# --------------------------------------------------------------------------- shared renderers


def test_renderers_match_api_output(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    _conclude_fact(client, project_id, "WordPress core is current; no known vulnerable plugins found.")

    for export_format, renderer in (("timeline", render_timeline), ("yaml", render_graph_yaml)):
        api_text = client.get(f"/projects/{project_id}/export?format={export_format}").text
        with db.get_conn() as conn:
            assert renderer(conn, project_id) == api_text

    assert "INTENT CONCLUDED" in client.get(f"/projects/{project_id}/export?format=timeline").text


def test_cli_export_matches_api_output(client: TestClient, tmp_path, monkeypatch) -> None:
    project_id = _seed_project(client)
    _conclude_fact(client, project_id, "fact one")

    monkeypatch.setattr(db, "_db_path", None)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["export", project_id, "--format", "timeline", "--out", "-", "--db-path", str(tmp_path / "board.db")],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == client.get(f"/projects/{project_id}/export?format=timeline").text


def test_export_unknown_project_is_clean_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["export", "proj_999", "--db-path", str(tmp_path / "x.db")])
    assert result.exit_code != 0
    assert "proj_999" in result.output


# --------------------------------------------------------------------------- evidence store


def test_evidence_registration_computes_hash_and_dedupes(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    fact_id = _conclude_fact(client, project_id, "auth flow mapped")

    evidence_file = projects_root / project_id / "evidence" / "i001" / "capture.txt"
    evidence_file.parent.mkdir(parents=True, exist_ok=True)
    evidence_file.write_text("GET /login HTTP/1.1\n", encoding="utf-8")
    expected_sha = hashlib.sha256(evidence_file.read_bytes()).hexdigest()

    response = client.post(
        f"/projects/{project_id}/evidence",
        json={"path": f"{project_id}/evidence/i001/capture.txt", "intent_id": "i001", "fact_id": fact_id},
    )
    assert response.status_code == 200
    row = response.json()
    assert row["id"] == "ev001"
    assert row["sha256"] == expected_sha
    assert row["size_bytes"] == len(evidence_file.read_bytes())
    assert row["kind"] == "notes"  # inferred from .txt

    duplicate = client.post(
        f"/projects/{project_id}/evidence",
        json={"path": f"{project_id}/evidence/i001/capture.txt", "sha256": expected_sha},
    )
    assert duplicate.json()["id"] == "ev001"

    listed = client.get(f"/projects/{project_id}/evidence").json()
    assert [item["id"] for item in listed] == ["ev001"]


def test_evidence_registration_rejects_unresolvable_file_without_sha(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    response = client.post(
        f"/projects/{project_id}/evidence",
        json={"path": f"{project_id}/evidence/i001/missing.bin"},
    )
    assert response.status_code == 400

    lost = client.post(
        f"/projects/{project_id}/evidence",
        json={"path": "/tmp/elp/i002_auth_flow_findings.md", "kind": "lost", "description": "wiped by reboot"},
    )
    assert lost.status_code == 200
    assert lost.json()["kind"] == "lost"
    assert lost.json()["sha256"] is None


def test_local_backend_durable_layout_and_env(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(evidence_root=str(tmp_path)))

    env = backend.worker_env("proj_042", "i007")
    evidence_dir = Path(env["BOARD_EVIDENCE_DIR"])
    assert evidence_dir == tmp_path / "proj_042" / "evidence" / "i007"
    assert evidence_dir.is_dir()  # created on first call

    artefact = evidence_dir / "shots" / "login.png"
    artefact.parent.mkdir(parents=True, exist_ok=True)
    artefact.write_bytes(b"\x89PNG fake")
    stored = backend.evidence_relative_path("proj_042", "i007", artefact)
    assert stored == "proj_042/evidence/i007/shots/login.png"

    handle = backend.ensure_running("proj_042")
    assert (Path(handle) / "workspace").is_dir()
    assert (Path(handle) / "reports").is_dir()
    assert (Path(handle) / "scratch").is_dir()

    default_backend = LocalBackend(LocalConfig())
    assert default_backend._root == default_projects_root()  # noqa: SLF001 - explicit default-root check


# --------------------------------------------------------------------------- findings


def test_findings_sorted_by_severity_with_negative_first_class(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    fact_low = _conclude_fact(client, project_id, "verbose banner on port 8080")
    fact_neg = _conclude_fact(client, project_id, "CVE-2025-29927 verified as patched")

    for payload in (
        {"fact_id": fact_neg, "title": "Next.js middleware bypass patched", "severity": "negative", "affected_target": "elp.envo-it.dk", "description": "Verified patched on the target."},
        {"fact_id": fact_low, "title": "Verbose service banner", "severity": "low", "description": "Banner discloses version info."},
    ):
        response = client.post(f"/projects/{project_id}/findings", json=payload)
        assert response.status_code == 201

    listed = client.get(f"/projects/{project_id}/findings").json()
    assert [item["severity"] for item in listed] == ["low", "negative"]
    assert listed[1]["status"] == "open"

    updated = client.put(
        f"/projects/{project_id}/findings/{listed[0]['id']}",
        json={"status": "mitigated"},
    )
    assert updated.json()["status"] == "mitigated"

    neg_only = client.get(f"/projects/{project_id}/findings?severity=negative").json()
    assert [item["title"] for item in neg_only] == ["Next.js middleware bypass patched"]


def test_findings_reject_unknown_fact(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    response = client.post(
        f"/projects/{project_id}/findings",
        json={"fact_id": "f999", "title": "x", "severity": "low", "description": "d"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- extraction


def test_findings_extraction_round_trip(client: TestClient, projects_root: Path, tmp_path, monkeypatch) -> None:
    project_id = _seed_project(client)
    _conclude_fact(
        client,
        project_id,
        "The login form at https://elp.envo-it.dk/login has no rate limiting. "
        "Full evidence trail in /tmp/elp/i002_auth_flow_findings.md. "
        "The WordPress installation is up to date and hardened.",
    )

    monkeypatch.setattr(db, "_db_path", None)
    runner = CliRunner()
    dry = runner.invoke(cli_main, ["findings", "extract", project_id, "--db-path", str(tmp_path / "board.db")])
    assert dry.exit_code == 0, dry.output
    assert "NEGATIVE" in dry.output  # hardened / up to date sentence
    assert "/tmp/elp/i002_auth_flow_findings.md" in dry.output  # lost evidence path detected

    applied = runner.invoke(cli_main, ["findings", "extract", project_id, "--apply", "--db-path", str(tmp_path / "board.db")])
    assert applied.exit_code == 0, applied.output

    listed = client.get(f"/projects/{project_id}/findings").json()
    assert any(item["severity"] == "negative" for item in listed)
    assert all(item["status"] == "needs_retest" for item in listed)

    evidence = client.get(f"/projects/{project_id}/evidence").json()
    lost_paths = [item["path"] for item in evidence if item["kind"] == "lost"]
    assert "/tmp/elp/i002_auth_flow_findings.md" in lost_paths

    # idempotent per fact: re-run inserts nothing new
    rerun = runner.invoke(cli_main, ["findings", "extract", project_id, "--apply", "--db-path", str(tmp_path / "board.db")])
    assert rerun.exit_code == 0
    assert client.get(f"/projects/{project_id}/findings").json() == listed


# --------------------------------------------------------------------------- reports


def test_agent_report_structure_and_absolute_paths(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    fact_id = _conclude_fact(client, project_id, "open admin panel at https://elp.envo-it.dk:8443/admin")

    client.post(
        f"/projects/{project_id}/findings",
        json={
            "fact_id": fact_id,
            "title": "Exposed admin panel",
            "severity": "high",
            "affected_target": "https://elp.envo-it.dk:8443",
            "description": "Admin panel reachable without authentication.",
            "recommendation": "Restrict by allowlist.",
        },
    )
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "requires foreign-tenant account (unobtainable)", "creator": "reasoner"},
    )

    evidence_file = projects_root / project_id / "evidence" / "i001" / "admin.png"
    evidence_file.parent.mkdir(parents=True, exist_ok=True)
    evidence_file.write_bytes(b"fake-png")
    client.post(
        f"/projects/{project_id}/evidence",
        json={"path": f"{project_id}/evidence/i001/admin.png", "intent_id": "i001", "fact_id": fact_id},
    )

    markdown = client.get(f"/projects/{project_id}/report?type=agent").text
    assert "Strikebox Agent Report" in markdown
    assert "Exposed admin panel" in markdown
    assert "unexecuted_intents" in markdown

    json_report = client.get(f"/projects/{project_id}/report?type=agent-json").json()
    header = json_report["header"]
    assert header["project_id"] == project_id
    assert header["evidence_root"] == str(projects_root)
    assert header["db_sha256"]
    assert json_report["findings"][0]["severity"] == "high"
    assert json_report["evidence"][0]["absolute_path"] == str(evidence_file)
    assert len(json_report["open_questions"]["unexecuted_intents"]) == 1
    assert "INTENT DECLARED" in json_report["timeline_text"]

    # embedded machine section is the same object as the JSON endpoint payload
    import json as jsonlib

    start = markdown.index("```json\n") + len("```json\n")
    end = markdown.index("\n```", start)
    assert jsonlib.loads(markdown[start:end])["header"] == header

    human = client.get(f"/projects/{project_id}/report?type=human").text
    assert "Executive summary" in human
    assert "Negative results" in human
    assert "Open questions" in human
    assert "evidence manifest" in human.lower()

    with db.get_conn() as conn:
        assert render_human_report(conn, project_id, evidence_root=projects_root)


def test_human_report_preserves_retired_details_outside_current_results(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    fact_id = _conclude_fact(client, project_id, "Candidate triage completed.")
    records = []
    for severity, status, title in (
        ("medium", "confirmed", "Current issue"),
        ("critical", "false_positive", "Rejected candidate"),
        ("high", "mitigated", "Resolved issue"),
    ):
        response = client.post(
            f"/projects/{project_id}/findings",
            json={
                "fact_id": fact_id,
                "title": title,
                "severity": severity,
                "description": f"Details for {title}.",
                "category": "auth",
                "affected_target": "/login",
                "recommendation": f"Recommendation for {title}.",
                "evidence_ids": ["ev001"],
            },
        )
        assert response.status_code == 201
        record = response.json()
        updated = client.put(f"/projects/{project_id}/findings/{record['id']}", json={"status": status})
        assert updated.status_code == 200
        records.append(updated.json())

    human = client.get(f"/projects/{project_id}/report?type=human").text
    current, history = human.split("## Appendix: disposition history", 1)
    assert "| critical | 0 |" in current
    assert "| high | 0 |" in current
    assert "| medium | 1 |" in current
    assert "issues for review and remediation" in current
    assert "severe issues that require immediate attention" not in current
    assert "### [MEDIUM] Current issue" in current
    for record in records[1:]:
        assert record["title"] not in current
        assert f"`{record['id']}`" in history
        assert f"status: {record['status']}" in history
        assert record["title"] in history
        assert record["description"] in history
        assert record["recommendation"] in history
        assert "target: `/login`" in history
        assert "category: auth" in history
        assert "**Evidence.** `ev001`" in history

    agent = client.get(f"/projects/{project_id}/report?type=agent-json").json()
    assert agent["header"]["finding_count"] == 3
    assert agent["findings"] == records
    assert {r["id"] for r in client.get(f"/projects/{project_id}/findings").json()} == {r["id"] for r in records}


@pytest.mark.parametrize("status", ["false_positive", "mitigated"])
@pytest.mark.parametrize("severity", ["critical", "negative"])
def test_human_report_with_only_retired_findings_has_no_current_claims(
    client: TestClient, projects_root: Path, status: str, severity: str,
) -> None:
    project_id = _seed_project(client)
    fact_id = _conclude_fact(client, project_id, "Candidate disposition recorded.")
    response = client.post(
        f"/projects/{project_id}/findings",
        json={"fact_id": fact_id, "title": "Historical result", "severity": severity, "status": status, "description": "Retired details."},
    )
    assert response.status_code == 201

    human = client.get(f"/projects/{project_id}/report?type=human").text
    current, history = human.split("## Appendix: disposition history", 1)
    assert "| critical | 0 |" in current
    assert "| negative (recorded) | 0 |" in current
    assert "No current findings remain" in current
    assert "No current vulnerability-class findings recorded" in current
    assert "No current negative results recorded" in current
    assert "Historical result" not in current
    assert "Historical result" in history
    assert "Retired details." in history
    assert f"status: {status}" in history
    assert "explicitly verified as solid" not in current


def test_human_report_keeps_active_and_legacy_statuses_without_title_deduplication(
    client: TestClient, projects_root: Path,
) -> None:
    project_id = _seed_project(client)
    fact_id = _conclude_fact(client, project_id, "Independent findings share a title.")
    active_records = []
    for status in ("open", "confirmed", "needs_retest", "blocked", None):
        response = client.post(
            f"/projects/{project_id}/findings",
            json={"fact_id": fact_id, "title": "Shared title", "severity": "high", "status": status or "open", "description": f"Details for {status}."},
        )
        assert response.status_code == 201
        record = response.json()
        if status is None:
            with db.get_conn() as conn:
                conn.execute("UPDATE findings SET status = NULL WHERE project_id = ? AND id = ?", (project_id, record["id"]))
        active_records.append(record)

    human = client.get(f"/projects/{project_id}/report?type=human").text
    current, history = human.split("## Appendix: disposition history", 1)
    assert "| high | 5 |" in current
    assert current.count("### [HIGH] Shared title") == 5
    for record in active_records:
        assert f"`{record['id']}`" in current
        assert record["description"] in current
        assert record["description"] not in history
    assert "status: needs_retest" in current
    assert "status: blocked" in current
    assert "No retired findings recorded" in history


def test_human_report_negative_results_retain_unresolved_statuses(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    fact_id = _conclude_fact(client, project_id, "Negative results need status-aware interpretation.")
    for status in ("open", "confirmed", "needs_retest", "blocked", None):
        response = client.post(
            f"/projects/{project_id}/findings",
            json={"fact_id": fact_id, "title": f"Negative result {status}", "severity": "negative", "status": status or "open", "description": "Recorded observation."},
        )
        assert response.status_code == 201
        if status is None:
            with db.get_conn() as conn:
                conn.execute("UPDATE findings SET status = NULL WHERE project_id = ? AND id = ?", (project_id, response.json()["id"]))

    human = client.get(f"/projects/{project_id}/report?type=human").text
    assert "| negative (recorded) | 5 |" in human
    coverage = human.split("## 4. Negative results & coverage", 1)[1].split("## 5.", 1)[0]
    for status in ("open", "confirmed", "needs_retest", "blocked", None):
        assert f"**Negative result {status}**" in coverage
        assert f"status: {status or 'open'}" in coverage
    assert "do not establish verified coverage" in coverage
    assert "tested and found solid" not in human
    assert "explicitly verified as solid" not in human
    assert "Negative results were recorded; see section 4 for their status and verification limits." in human


def test_worker_finding_disposition_survives_ingestion_and_human_reporting(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    fact_id = _conclude_fact(client, project_id, "Worker rejected a candidate with evidence.")
    _, _, findings, _ = validate_explore_payload({
        "accepted": True,
        "data": {
            "description": "Candidate was rejected.",
            "findings": [{"title": "Worker candidate", "severity": "high", "status": "false_positive", "description": "Evidence rules out the suspected weakness."}],
        },
    })

    class FindingsClient:
        def create_finding(self, pid: str, payload: dict) -> ApiResult:
            response = client.post(f"/projects/{pid}/findings", json=payload)
            return ApiResult(status_code=response.status_code, data=response.json(), text=response.text)

    assert insert_worker_findings(FindingsClient(), project_id, "i001", fact_id, findings) == 1
    stored = client.get(f"/projects/{project_id}/findings").json()
    assert stored[0]["status"] == "false_positive"
    agent = client.get(f"/projects/{project_id}/report?type=agent-json").json()
    assert agent["findings"] == stored

    human = client.get(f"/projects/{project_id}/report?type=human").text
    current, history = human.split("## Appendix: disposition history", 1)
    assert "| high | 0 |" in current
    assert "Worker candidate" not in current
    assert "Worker candidate" in history
    assert "status: false_positive" in history
    assert "Evidence rules out the suspected weakness." in history


def test_report_unknown_project_is_404(client: TestClient) -> None:
    assert client.get("/projects/proj_999/report?type=human").status_code == 404
    assert client.get("/projects/proj_999/report?type=bogus").status_code == 400


def test_agent_report_json_renderer_stable(client: TestClient, projects_root: Path) -> None:
    project_id = _seed_project(client)
    with db.get_conn() as conn:
        report = build_agent_report(conn, project_id, evidence_root=projects_root)
    text = render_agent_report_json(report)
    assert text.endswith("}\n")
    assert '"negative_results": []' in text
