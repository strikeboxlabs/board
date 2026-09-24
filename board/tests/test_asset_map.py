"""Attack-surface asset map: extraction, renderers, API, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from board.cli import main as cli_main
from board.reporting.assets import build_asset_map
from board.server import db
from board.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "board.db")
    with TestClient(app) as test_client:
        yield test_client


def _seed(client: TestClient, description: str) -> str:
    response = client.post(
        "/projects",
        json={"title": "acme external", "origin": "acme.com", "goal": "assess posture"},
    )
    project_id = response.json()["project"]["id"]
    intent = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "recon", "creator": "explorer", "worker": "explorer"},
    ).json()["id"]
    client.post(
        f"/projects/{project_id}/intents/{intent}/conclude",
        json={"worker": "explorer", "description": description},
    )
    return project_id


PERIMETER_FACT = (
    "elp.acme.com is fully served by Railway edge proxies at 69.46.46.26, bypassing Cloudflare completely. "
    "The API endpoint https://elp.acme.com/api/users returns 401 without auth. "
    "elp.acme.com runs Next.js 14.2. "
    "vpn.acme.com:4443 is an internet-exposed FortiGate SSL-VPN portal on 86.48.38.27. "
    "CVE-2025-29927 was tested and patched. "
    "Procurement data is hosted at acme.coupahost.com. "
    "Our probe host evil-probe-9x7.example.com and FUZZ.acme.com were used for testing; "
    "artifacts in openapi.json and Class.create.constructor were ignored as assets."
)


def _nodes_by_type(asset_map: dict) -> dict[str, list[dict]]:
    by_type: dict[str, list[dict]] = {}
    for node in asset_map["nodes"]:
        by_type.setdefault(node["type"], []).append(node)
    return by_type


def test_extraction_builds_typed_graph_with_provenance(client: TestClient) -> None:
    project_id = _seed(client, PERIMETER_FACT)
    with db.get_conn() as conn:
        asset_map = build_asset_map(conn, project_id)

    nodes = {node["id"]: node for node in asset_map["nodes"]}
    by_type = _nodes_by_type(asset_map)

    # core assets
    assert "domain:elp.acme.com" in nodes
    assert "ip:69.46.46.26" in nodes
    assert "ip:86.48.38.27" in nodes
    assert "tech:Next.js" in nodes
    assert nodes["tech:Next.js"]["version"] == "14.2"
    assert "cve:CVE-2025-29927" in nodes
    assert "cloud:Cloudflare WAF/CDN" in nodes
    assert "cloud:Railway" in nodes
    assert "tech:FortiGate SSL-VPN" in nodes
    assert "service:vpn.acme.com:4443" in nodes

    # endpoint extracted with parent link
    endpoints = [n for n in by_type.get("endpoint", []) if n["label"] == "https://elp.acme.com/api/users"]
    assert endpoints and endpoints[0]["endpoint_class"] == "api"

    # noise filtered, probes classified
    labels = {node["label"] for node in asset_map["nodes"]}
    assert "openapi.json" not in labels
    assert "Class.create.constructor" not in labels
    assert "domain:evil-probe-9x7.example.com" not in nodes
    assert "probe:evil-probe-9x7.example.com" in nodes
    assert "probe:FUZZ.acme.com" in nodes

    # relations with provenance
    edges = {(e["source"], e["target"], e["type"]) for e in asset_map["edges"]}
    assert ("domain:elp.acme.com", "ip:69.46.46.26", "served_by") in edges
    assert ("domain:elp.acme.com", "cloud:Railway", "hosted_on") in edges
    assert ("domain:elp.acme.com", "tech:Next.js", "runs") in edges
    assert ("domain:vpn.acme.com", "service:vpn.acme.com:4443", "exposes") in edges
    assert ("domain:elp.acme.com", "domain:acme.com", "belongs_to") in edges  # >=2 subdomains -> parent node

    # every node carries fact provenance (except synthetic parents)
    for node in asset_map["nodes"]:
        if not node.get("synthetic"):
            assert node["fact_ids"], f"node without provenance: {node['id']}"
    # fact text + evidence slot present
    assert "Railway" in asset_map["facts"]["f001"]["text"]

    # list categories and groups
    assert nodes["domain:elp.acme.com"]["category"] == "domain"
    assert nodes["domain:elp.acme.com"]["group"] == "acme.com"
    assert nodes["cloud:Railway"]["category"] == "cloud"
    assert nodes["cloud:Railway"]["group"] == "Railway"
    assert nodes["cloud:Coupa"]["category"] == "saas"  # external SaaS holding company data
    assert nodes["cloud:Coupa"]["group"] == "Coupa"
    assert "domain:acme.coupahost.com" in nodes
    assert asset_map["summary"]["categories"]["domain"] >= 2


def test_intents_touching_assets_are_recorded(client: TestClient) -> None:
    project_id = _seed(client, PERIMETER_FACT)
    # a second, still-open intent whose description mentions the asset
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["f001"], "description": "deeper look at elp.acme.com origin", "creator": "reasoner"},
    )

    with db.get_conn() as conn:
        asset_map = build_asset_map(conn, project_id)
    nodes = {node["id"]: node for node in asset_map["nodes"]}

    # i001 produced f001 (the establishing fact); i002's description mentions the asset
    assert nodes["domain:elp.acme.com"]["intent_ids"] == ["i001", "i002"]
    # producing intent recorded on the fact, open flag on the mentioning intent
    assert asset_map["facts"]["f001"]["intent_id"] == "i001"
    assert asset_map["intents"]["i002"]["open"] is True
    assert "origin" in asset_map["intents"]["i002"]["description"]

    markdown = open_asset_markdown(client, project_id)
    assert "intents: i001, i002" in markdown


def open_asset_markdown(client: TestClient, project_id: str) -> str:
    return client.get(f"/projects/{project_id}/asset-map?format=md").text


def test_findings_anchor_severity_on_assets(client: TestClient) -> None:
    project_id = _seed(client, PERIMETER_FACT)
    client.post(
        f"/projects/{project_id}/findings",
        json={
            "fact_id": "f001",
            "title": "Origin bypass",
            "severity": "high",
            "affected_target": "elp.acme.com",
            "description": "Direct origin access elp.acme.com",
        },
    )
    client.post(
        f"/projects/{project_id}/findings",
        json={
            "fact_id": "f001",
            "title": "CVE patched",
            "severity": "negative",
            "affected_target": "CVE-2025-29927",
            "description": "CVE-2025-29927 not exploitable",
        },
    )
    with db.get_conn() as conn:
        asset_map = build_asset_map(conn, project_id)
    nodes = {node["id"]: node for node in asset_map["nodes"]}
    assert nodes["domain:elp.acme.com"]["severity"] == "high"
    assert "fnd001" in nodes["domain:elp.acme.com"]["finding_ids"]
    assert nodes["cve:CVE-2025-29927"]["negative_count"] == 1


def test_private_and_resolver_ips_are_not_assets(client: TestClient) -> None:
    project_id = _seed(client, "Resolver 8.8.8.8 and internal host 192.168.1.10 plus metadata 169.254.169.254 were probed. Public edge is 69.46.46.26.")
    with db.get_conn() as conn:
        asset_map = build_asset_map(conn, project_id)
    ips = [node["label"] for node in asset_map["nodes"] if node["type"] == "ip"]
    assert ips == ["69.46.46.26"]


def test_api_renders_all_formats(client: TestClient) -> None:
    project_id = _seed(client, PERIMETER_FACT)

    html = client.get(f"/projects/{project_id}/asset-map?format=html")
    assert html.status_code == 200
    assert "Attack Surface —" in html.text
    assert "cytoscape" in html.text  # self-contained: library inlined
    assert "elp.acme.com" in html.text
    # lists-first design: inventory is the primary view
    assert "Inventory" in html.text
    assert "All assets" in html.text  # sidebar
    assert "Download CSV" in html.text
    assert "Third-party SaaS" in html.text  # category present
    assert "Findings / Facts / Intents" in html.text
    assert "Intents touching this asset" in html.text

    graph = client.get(f"/projects/{project_id}/asset-map?format=json").json()
    assert graph["summary"]["node_count"] > 5
    assert graph["project"]["id"] == project_id
    assert graph["summary"]["categories"]["saas"] >= 1

    markdown = client.get(f"/projects/{project_id}/asset-map?format=md").text
    assert "Attack Surface Inventory" in markdown
    assert "elp.acme.com" in markdown
    assert "Domains & subdomains" in markdown
    assert "Third-party SaaS" in markdown
    assert "### acme.com" in markdown  # subdomains grouped under parent domain

    assert client.get(f"/projects/{project_id}/asset-map?format=bogus").status_code == 400
    assert client.get("/projects/proj_999/asset-map").status_code == 404


def test_cli_assets_writes_three_files(client: TestClient, tmp_path, monkeypatch) -> None:
    project_id = _seed(client, PERIMETER_FACT)
    monkeypatch.setenv("BOARD_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(db, "_db_path", None)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["assets", project_id, "--db-path", str(tmp_path / "board.db")])
    assert result.exit_code == 0, result.output

    reports = tmp_path / "projects" / project_id / "reports"
    assert sorted(p.name for p in reports.iterdir()) == ["asset-inventory.md", "asset-map.html", "asset-map.json"]
    graph = json.loads((reports / "asset-map.json").read_text())
    assert graph["summary"]["counts"].get("domain", 0) >= 2
    assert (reports / "asset-map.html").stat().st_size > 100_000  # cytoscape inlined

    stdout_only = runner.invoke(
        cli_main, ["assets", project_id, "--format", "json", "--out", "-", "--db-path", str(tmp_path / "board.db")]
    )
    assert stdout_only.exit_code == 0
    assert json.loads(stdout_only.stdout)["project"]["id"] == project_id
