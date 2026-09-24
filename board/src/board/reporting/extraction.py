"""Rule-based findings extraction from fact prose (Change 3.2, worker-free path).

Dense fact descriptions are excellent LLM input but useless for sorting and
severity-ranking. This module retro-fits prose-heavy runs (e.g. a completed
pentest whose facts were never structured) by parsing each fact description
into candidate findings. It is deliberately conservative and auditable:
run it dry first, review, then apply. Facts that already have findings
rows are skipped, so extraction is idempotent per fact.

It also detects referenced-but-missing artefact paths (e.g. ``/tmp/...``
evidence that was wiped by a reboot) so they can be registered as
``kind='lost'`` evidence rows — the honest historical record.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

SEVERITY_BY_WORD = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "moderate": "medium",
    "low": "low",
    "minor": "low",
    "info": "info",
    "informational": "info",
}

NEGATIVE_MARKERS = (
    "not vulnerable",
    "no vulnerability",
    "not affected",
    "out of affected",
    "unaffected",
    "patched",
    "hardened",
    "no open redirect",
    "not exploitable",
    "no misconfiguration",
    "not exposed",
    "no sensitive",
    "properly configured",
    "securely configured",
    "up to date",
    "up-to-date",
    "current version",
    "no evidence of",
    "found solid",
    "no findings",
    "not disclosed",
    "no disclosure",
    "disabled",
    "blocked brute",
    "strong ",
    "rate limited",
)

CATEGORY_KEYWORDS = {
    "auth": ("password", "login", "authentication", "session", "jwt", "mfa", "2fa", "auth", "account", "credential"),
    "crypto": ("ssl", "tls", "certificate", "cipher", "encryption", "crypto", "https"),
    "disclosure": ("header", "cors", "spf", "dmarc", "dkim", "information leakage", "version disclosure", "enumerate", "enumeration", "exposed data"),
    "perimeter": ("firewall", "port", "perimeter", "waf", "fortigate", "ids", "ips", "network", "dns"),
    "config": ("configuration", "config", "backup", "debug", "default", "admin panel", "git exposure"),
}

CVE_PATTERN = re.compile(r"\bCVE-\d{4}-\d{4,8}\b", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s)\]}>\"',]+")
HOST_PATTERN = re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b")
IP_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
PATH_PATTERN = re.compile(r"(?<![\w])/(?:tmp|var|home|root|opt|data|mnt)/[\w./-]+")
TITLE_MAX = 100
MIN_SEGMENT_LENGTH = 25
MAX_FINDINGS_PER_FACT = 10


@dataclass(slots=True)
class ExtractedFinding:
    fact_id: str
    intent_id: str | None
    title: str
    severity: str
    category: str | None = None
    affected_target: str | None = None
    description: str | None = None
    recommendation: str | None = None

    def to_payload(self) -> dict:
        payload = {
            "fact_id": self.fact_id,
            "title": self.title,
            "severity": self.severity,
            "description": self.description or self.title,
        }
        if self.intent_id:
            payload["intent_id"] = self.intent_id
        if self.category:
            payload["category"] = self.category
        if self.affected_target:
            payload["affected_target"] = self.affected_target
        if self.recommendation:
            payload["recommendation"] = self.recommendation
        return payload


@dataclass(slots=True)
class ExtractionResult:
    findings: list[ExtractedFinding] = field(default_factory=list)
    lost_evidence: list[dict] = field(default_factory=list)
    skipped_facts: list[str] = field(default_factory=list)
    scanned_facts: int = 0


def extract_findings(conn: sqlite3.Connection, project_id: str, *, lost_paths_exist: bool = False) -> ExtractionResult:
    """Extract candidate findings from all fact prose of a project.

    ``lost_paths_exist``: when True, referenced paths that do exist on disk are
    ignored; when False (default) any referenced path is treated as lost only
    if it is missing — checked lazily by the caller via Path.exists if desired.
    """
    del lost_paths_exist  # existence is checked by the caller when applying
    result = ExtractionResult()

    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ? AND id NOT IN ('origin', 'goal')",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT id, to_fact_id FROM intents WHERE project_id = ? AND to_fact_id IS NOT NULL",
        (project_id,),
    ).fetchall()
    intent_by_fact = {row["to_fact_id"]: row["id"] for row in intents}

    facts_with_findings = {
        row["fact_id"]
        for row in conn.execute(
            "SELECT DISTINCT fact_id AS fact_id FROM findings WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    }

    for fact in facts:
        result.scanned_facts += 1
        if fact["id"] in facts_with_findings:
            result.skipped_facts.append(fact["id"])
            continue
        produced = _segments_to_findings(fact["id"], intent_by_fact.get(fact["id"]), fact["description"])
        result.findings.extend(produced[:MAX_FINDINGS_PER_FACT])
        result.lost_evidence.extend(_referenced_missing_paths(fact["id"], intent_by_fact.get(fact["id"]), fact["description"]))

    return result


def apply_extraction(conn: sqlite3.Connection, project_id: str, result: ExtractionResult, *, utcnow: str) -> tuple[int, int]:
    """Insert extraction results. Returns ``(findings_inserted, lost_evidence_inserted)``.

    Missing-path evidence rows are only registered for paths that are actually
    absent from disk; existing artefacts are left to normal registration.
    """
    from pathlib import Path

    from board.server.services import next_evidence_id, next_finding_id

    findings_inserted = 0
    for finding in result.findings:
        fid = next_finding_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO findings (
                id, project_id, fact_id, intent_id, title, category, severity, status,
                affected_target, description, recommendation, evidence_ids, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'needs_retest', ?, ?, ?, '[]', ?, ?)
            """,
            (
                fid,
                project_id,
                finding.fact_id,
                finding.intent_id,
                finding.title,
                finding.category,
                finding.severity,
                finding.affected_target,
                finding.description,
                finding.recommendation,
                utcnow,
                utcnow,
            ),
        )
        findings_inserted += 1

    lost_inserted = 0
    for item in result.lost_evidence:
        path = item["path"]
        if Path(path).exists():
            continue
        if _evidence_row_exists(conn, project_id, path):
            continue
        evid = next_evidence_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO evidence (id, project_id, intent_id, fact_id, path, kind, sha256, size_bytes, created_at, description)
            VALUES (?, ?, ?, ?, ?, 'lost', NULL, NULL, ?, ?)
            """,
            (evid, project_id, item.get("intent_id"), item["fact_id"], path, utcnow, item["description"]),
        )
        lost_inserted += 1

    return findings_inserted, lost_inserted


def _evidence_row_exists(conn: sqlite3.Connection, project_id: str, path: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM evidence WHERE project_id = ? AND path = ?",
            (project_id, path),
        ).fetchone()
        is not None
    )


def _split_segments(text: str) -> list[str]:
    segments: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if not line:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = sentence.strip()
            if len(sentence) >= MIN_SEGMENT_LENGTH:
                segments.append(sentence)
    return segments


def _segments_to_findings(fact_id: str, intent_id: str | None, description: str) -> list[ExtractedFinding]:
    findings: list[ExtractedFinding] = []
    for segment in _split_segments(description):
        severity = _classify_severity(segment)
        if severity is None:
            continue
        title = _make_title(segment)
        findings.append(
            ExtractedFinding(
                fact_id=fact_id,
                intent_id=intent_id,
                title=title,
                severity=severity,
                category=_infer_category(segment),
                affected_target=_infer_target(segment),
                description=segment,
            )
        )
    return findings


def _classify_severity(segment: str) -> str | None:
    lowered = segment.lower()
    for marker in NEGATIVE_MARKERS:
        if marker in lowered:
            return "negative"
    for word, severity in SEVERITY_BY_WORD.items():
        if re.search(rf"\b{word}\b", lowered):
            return severity
    if CVE_PATTERN.search(segment) and not re.search(r"\b(no|not|patched|fixed|unaffected)\b", lowered[:60]):
        return "high"
    return None


def _make_title(segment: str) -> str:
    title = segment.strip()
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "…"
    return title


def _infer_category(segment: str) -> str | None:
    lowered = segment.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


def _infer_target(segment: str) -> str | None:
    for pattern in (URL_PATTERN, CVE_PATTERN, IP_PATTERN):
        match = pattern.search(segment)
        if match:
            return match.group(0)
    for match in HOST_PATTERN.finditer(segment):
        host = match.group(0)
        if "." in host and not host.isdigit():
            return host
    return None


def _referenced_missing_paths(fact_id: str, intent_id: str | None, description: str) -> list[dict]:
    entries = []
    seen: set[str] = set()
    for match in PATH_PATTERN.finditer(description):
        path = match.group(0).rstrip(".,;:)")
        if path in seen:
            continue
        seen.add(path)
        entries.append(
            {
                "fact_id": fact_id,
                "intent_id": intent_id,
                "path": path,
                "description": f"Referenced by fact {fact_id} but not registered before the artefact was lost (e.g. wiped /tmp).",
            }
        )
    return entries
