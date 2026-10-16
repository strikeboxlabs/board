"""Agent report renderer — machine-first output for AI consumers.

Design rule (from the reporting directive): everything an agent needs to
act, nothing it must re-parse by guessing. Stable field names, absolute
evidence paths, and an explicit negative-results list. The JSON object
embedded in the markdown report is byte-identical to ``report-agent.json``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from board import __version__
from board.reporting.data import load_evidence, load_findings, load_project_data
from board.reporting.timeline import render_timeline


def build_agent_report(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    evidence_root: Path | str,
    db_sha256: str | None = None,
) -> dict:
    data = load_project_data(conn, project_id)
    root = Path(evidence_root).expanduser()

    facts_by_id = {f["id"]: f["description"] for f in data.facts}
    origin = facts_by_id.get("origin", "")
    goal = facts_by_id.get("goal", "")

    concluded_times = [i["concluded_at"] for i in data.intents if i["concluded_at"]]
    time_window = {
        "start": data.project["created_at"],
        "end": max(concluded_times) if concluded_times else data.project["created_at"],
    }

    findings = [_finding_row_to_dict(row) for row in load_findings(conn, project_id)]
    negative = [f for f in findings if f["severity"] == "negative"]

    evidence = [_evidence_row_to_dict(row, root) for row in load_evidence(conn, project_id)]

    provenance_intents = []
    for i in data.intents:
        provenance_intents.append(
            {
                "id": i["id"],
                "from": data.sources_by_intent.get(i["id"], []),
                "to": i["to_fact_id"],
                "description": i["description"],
                "creator": i["creator"],
                "worker": i["worker"],
                "created_at": i["created_at"],
                "concluded_at": i["concluded_at"],
            }
        )

    unexecuted = [
        {
            "id": i["id"],
            "description": i["description"],
            "from": data.sources_by_intent.get(i["id"], []),
            "creator": i["creator"],
            "declared_at": i["created_at"],
            "worker": i["worker"],
        }
        for i in data.intents
        if i["to_fact_id"] is None
    ]

    return {
        "header": {
            "project_id": project_id,
            "title": data.project["title"],
            "status": data.project["status"],
            "completion_outcome": _column(data.project, "completion_outcome"),
            "abort_reason": _column(data.project, "abort_reason"),
            "aborted_at": _column(data.project, "aborted_at"),
            "aborted_by": _column(data.project, "aborted_by"),
            "origin": origin,
            "goal": goal,
            "created_at": data.project["created_at"],
            "time_window": time_window,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "board_version": __version__,
            "evidence_root": str(root),
            "db_sha256": db_sha256,
            "finding_count": len(findings),
            "evidence_count": len(evidence),
        },
        "findings": findings,
        "negative_results": negative,
        "evidence": evidence,
        "provenance": {
            "facts": [{"id": f["id"], "description": f["description"]} for f in data.facts],
            "intents": provenance_intents,
        },
        "open_questions": {
            "note": "Intents that were declared but never concluded (never executed, or abandoned/blocked).",
            "unexecuted_intents": unexecuted,
        },
        "timeline_text": render_timeline(conn, project_id),
    }


def render_agent_report_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def render_agent_report_markdown(report: dict) -> str:
    header = report["header"]
    lines: list[str] = []
    lines.append(f"# Strikebox Agent Report — {header['project_id']}: {header['title']}")
    lines.append("")
    lines.append("Machine-readable report generated from the Strikebox blackboard. The JSON block below is the complete structured payload; the prose sections after it are convenience renderings of the same data.")
    lines.append("")
    lines.append(f"- Project: `{header['project_id']}` ({header['status']})")
    if header.get("completion_outcome") == "current_results":
        lines.append("- Outcome: concluded with current results; the original goal is not asserted to have been achieved.")
    lines.append(f"- Goal: {header['goal']}")
    lines.append(f"- Time window: {header['time_window']['start']} → {header['time_window']['end']}")
    lines.append(f"- Generated: {header['generated_at']} by Strikebox {header['board_version']}")
    lines.append(f"- Evidence root: `{header['evidence_root']}`")
    if header.get("db_sha256"):
        lines.append(f"- Blackboard DB sha256 at generation: `{header['db_sha256']}`")
    lines.append(f"- Findings: {header['finding_count']} (incl. {len(report['negative_results'])} negative results) · Evidence artefacts: {header['evidence_count']}")
    lines.append("")
    lines.append("## Machine section")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(report, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## Methodology timeline")
    lines.append("")
    lines.append("```text")
    lines.append(report["timeline_text"].rstrip("\n"))
    lines.append("```")
    lines.append("")
    lines.append("## Findings detail")
    lines.append("")
    if not report["findings"]:
        lines.append("_No structured findings recorded for this project._")
    for finding in report["findings"]:
        lines.append(f"### [{finding['severity'].upper() if finding['severity'] else 'INFO'}] {finding['id']} — {finding['title']}")
        lines.append("")
        target = finding.get("affected_target")
        if target:
            lines.append(f"- Target: `{target}`")
        if finding.get("category"):
            lines.append(f"- Category: {finding['category']}")
        lines.append(f"- Status: {finding.get('status') or 'open'} · Established by fact `{finding['fact_id']}`" + (f" (intent `{finding['intent_id']}`)" if finding.get("intent_id") else ""))
        lines.append("")
        lines.append(finding["description"])
        if finding.get("recommendation"):
            lines.append("")
            lines.append(f"**Recommendation:** {finding['recommendation']}")
        if finding.get("evidence_ids"):
            lines.append("")
            lines.append(f"**Evidence:** {', '.join(f'`{e}`' for e in finding['evidence_ids'])}")
        lines.append("")
    return "\n".join(lines)


def _column(row, name: str):
    return row[name] if name in row.keys() else None


def _finding_row_to_dict(row) -> dict:
    raw_ids = row["evidence_ids"]
    evidence_ids: list[str] = []
    if raw_ids:
        try:
            parsed = json.loads(raw_ids)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            evidence_ids = [str(item) for item in parsed]
    return {
        "id": row["id"],
        "fact_id": row["fact_id"],
        "intent_id": row["intent_id"],
        "title": row["title"],
        "category": row["category"],
        "severity": row["severity"],
        "status": row["status"],
        "affected_target": row["affected_target"],
        "description": row["description"],
        "recommendation": row["recommendation"],
        "evidence_ids": evidence_ids,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _evidence_row_to_dict(row, evidence_root: Path) -> dict:
    path = row["path"]
    absolute = Path(path)
    if not absolute.is_absolute():
        absolute = evidence_root / path
    return {
        "id": row["id"],
        "path": path,
        "absolute_path": str(absolute),
        "kind": row["kind"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "intent_id": row["intent_id"],
        "fact_id": row["fact_id"],
        "created_at": row["created_at"],
        "description": row["description"],
    }
