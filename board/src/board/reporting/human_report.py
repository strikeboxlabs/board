"""Human report renderer — readable delivery document.

Sections: executive summary, findings by severity, methodology timeline,
negative results & coverage, open questions, disposition history and evidence appendices.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

from board.reporting.agent_report import build_agent_report


def render_human_report(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    evidence_root: Path | str,
    db_sha256: str | None = None,
) -> str:
    report = build_agent_report(conn, project_id, evidence_root=evidence_root, db_sha256=db_sha256)
    header = report["header"]
    root = Path(evidence_root).expanduser()

    retired_statuses = {"false_positive", "mitigated"}
    findings = [f for f in report["findings"] if f.get("status") not in retired_statuses]
    retired_findings = [f for f in report["findings"] if f.get("status") in retired_statuses]
    counts = Counter(f["severity"] or "info" for f in findings)
    vuln_findings = [f for f in findings if f["severity"] != "negative"]
    negative_findings = [f for f in findings if f["severity"] == "negative"]

    lines: list[str] = []
    lines.append(f"# Security Assessment Report — {header['title']}")
    lines.append("")
    if header.get("status") == "aborted" and header.get("abort_reason"):
        status_line = f"**Project:** `{header['project_id']}` · **Status:** aborted — {header['abort_reason']}"
    else:
        status_line = f"**Project:** `{header['project_id']}` · **Status:** {header['status']}"
    lines.append(status_line + "  ")
    if header.get("completion_outcome") == "current_results":
        lines.append("**Outcome:** concluded with current results. The original goal is not asserted to have been achieved; unresolved questions and coverage gaps remain.  ")
    lines.append(f"**Period:** {header['time_window']['start']} → {header['time_window']['end']}  ")
    lines.append(f"**Generated:** {header['generated_at']}")
    lines.append("")

    lines.append("## 1. Executive summary")
    lines.append("")
    lines.append(f"**Goal.** {header['goal']}")
    lines.append("")
    lines.append(
        f"**Scope & method.** Strikebox explored the objective below using an autonomous fact/intent search "
        f"({len(report['provenance']['intents'])} intents declared, {len(report['provenance']['facts'])} facts recorded). "
        f"The full methodology timeline is reproduced in section 3."
    )
    origin = header.get("origin")
    if origin:
        lines.append("")
        lines.append(f"**Origin.** {origin}")
    lines.append("")
    lines.append("**Current results by severity.** False positives and mitigated findings are excluded; their records are preserved in the disposition history appendix.")
    lines.append("")
    lines.append("| Severity | Count |")
    lines.append("|---|---|")
    for severity in ("critical", "high", "medium", "low", "info"):
        lines.append(f"| {severity} | {counts.get(severity, 0)} |")
    lines.append(f"| negative (recorded) | {counts.get('negative', 0)} |")
    lines.append("")
    aborted = header.get("status") == "aborted"
    if aborted:
        when = f" ({header.get('aborted_at', '')})" if header.get("aborted_at") else ""
        label = "**Outcome.**"
        posture = (
            f"The engagement was aborted before the goal was reached{when}: "
            f"{header['abort_reason']}. The results below are what was established before "
            "the abort; section 5 lists the paths that were never executed."
        )
    elif counts.get("critical", 0) or counts.get("high", 0):
        label = "**Overall posture.**"
        posture = "Current records include high or critical severity findings; review their status and supporting evidence."
    elif counts.get("medium", 0) or counts.get("low", 0):
        label = "**Overall posture.**"
        posture = "Current records include issues for review and remediation; see section 2 for their status and evidence."
    elif negative_findings:
        label = "**Overall posture.**"
        posture = "Negative results were recorded; see section 4 for their status and verification limits."
    elif vuln_findings:
        label = "**Overall posture.**"
        posture = "The assessment recorded informational findings; see section 2 for details."
    elif retired_findings:
        label = "**Overall posture.**"
        posture = "No current findings remain; prior findings and their dispositions are preserved in the history appendix."
    else:
        label = "**Overall posture.**"
        posture = "No structured findings were recorded for this run; see the methodology timeline for what was examined."
    lines.append(f"{label} {posture}")
    lines.append("")

    lines.append("## 2. Findings by severity")
    lines.append("")
    if not vuln_findings:
        lines.append("_No current vulnerability-class findings recorded._")
        lines.append("")
    for finding in vuln_findings:
        _append_finding(lines, finding)

    lines.append("## 3. Methodology timeline")
    lines.append("")
    lines.append("```text")
    lines.append(report["timeline_text"].rstrip("\n"))
    lines.append("```")
    lines.append("")

    lines.append("## 4. Negative results & coverage")
    lines.append("")
    lines.append("Recorded observations of absence of weakness, with their current status. Blocked results and results needing retest do not establish verified coverage.")
    lines.append("")
    if not negative_findings:
        lines.append("_No current negative results recorded._")
    for finding in negative_findings:
        target = f" — `{finding['affected_target']}`" if finding.get("affected_target") else ""
        status = finding.get("status") or "open"
        lines.append(f"- **{finding['title']}**{target} (`{finding['id']}`, status: {status}): {finding['description']}")
    lines.append("")

    lines.append("## 5. Open questions & blocked paths")
    lines.append("")
    unexecuted = report["open_questions"]["unexecuted_intents"]
    if not unexecuted:
        lines.append("_No intents were left unexecuted._")
    else:
        lines.append("The following intents were declared but never concluded (never executed, or abandoned due to blockers):")
        lines.append("")
        for intent in unexecuted:
            worker = f", last worker: {intent['worker']}" if intent.get("worker") else ""
            lines.append(f"- `{intent['id']}` (declared {intent['declared_at']}{worker}): {intent['description']}")
    lines.append("")

    lines.append("## Appendix: disposition history")
    lines.append("")
    lines.append("False positives and mitigated findings are retained here for traceability. They do not contribute to current severity counts, posture, or coverage conclusions.")
    lines.append("")
    if not retired_findings:
        lines.append("_No retired findings recorded._")
        lines.append("")
    for finding in retired_findings:
        _append_finding(lines, finding)

    lines.append("## Appendix: evidence manifest")
    lines.append("")
    lines.append(f"All artefacts live under `{root}`; `path` values are relative to that root.")
    lines.append("")
    if not report["evidence"]:
        lines.append("_No evidence artefacts registered._")
    else:
        lines.append("| ID | Kind | Path | sha256 | Size | Intent |")
        lines.append("|---|---|---|---|---|---|")
        for item in report["evidence"]:
            size = "-" if item["size_bytes"] is None else f"{item['size_bytes']:,} B"
            sha = (item["sha256"] or "-")[:12]
            lines.append(
                f"| `{item['id']}` | {item['kind'] or '-'} | `{item['path']}` | `{sha}` | {size} | {item['intent_id'] or '-'} |"
            )
    lines.append("")
    return "\n".join(lines)


def _append_finding(lines: list[str], finding: dict) -> None:
    lines.append(f"### [{(finding['severity'] or 'info').upper()}] {finding['title']}")
    lines.append("")
    meta = [f"`{finding['id']}`"]
    if finding.get("affected_target"):
        meta.append(f"target: `{finding['affected_target']}`")
    if finding.get("category"):
        meta.append(f"category: {finding['category']}")
    meta.append(f"status: {finding.get('status') or 'open'}")
    lines.append(" · ".join(meta))
    lines.append("")
    lines.append(finding["description"])
    lines.append("")
    if finding.get("recommendation"):
        lines.append(f"**Recommendation.** {finding['recommendation']}")
        lines.append("")
    if finding.get("evidence_ids"):
        lines.append(f"**Evidence.** {', '.join(f'`{e}`' for e in finding['evidence_ids'])} (see appendix)")
        lines.append("")
