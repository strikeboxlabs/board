"""Summarize measured usage without treating activity as proof of goal progress."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path


def summarize(path: Path) -> dict:
    usage = Counter()
    phases = Counter()
    outcomes = Counter()
    elapsed = Counter()
    timestamps = []
    measured = missing = malformed = 0
    last_graph = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            stamp = datetime.fromisoformat(entry["timestamp"])
        except (ValueError, KeyError, TypeError):
            malformed += 1
            continue
        timestamps.append(stamp)
        if entry.get("event") == "process":
            phase = entry["phase"]
            phases[phase] += 1
            elapsed[phase] += entry.get("elapsed_seconds", 0)
            if entry.get("usage_available"):
                measured += 1
                usage.update(entry.get("usage", {}))
            else:
                missing += 1
        elif entry.get("event") == "task":
            outcomes[f'{entry["task_type"]}:{entry["outcome"]}'] += 1
            last_graph = {k: entry[k] for k in ("project_status", "facts", "closed_intents", "open_intents") if k in entry}
    conclusions = outcomes["explore:success"]
    total = usage.get("totalTokens", 0)
    return {
        "window_start": min(timestamps).isoformat() if timestamps else None,
        "window_end": max(timestamps).isoformat() if timestamps else None,
        "wall_seconds": (max(timestamps) - min(timestamps)).total_seconds() if timestamps else 0,
        "process_runs": dict(phases), "process_seconds": dict(elapsed),
        "task_outcomes": dict(outcomes), "tokens": dict(usage),
        "processes_with_usage": measured, "processes_without_usage": missing,
        "malformed_lines": malformed, "latest_graph": last_graph,
        "successful_explore_tasks_per_million_recorded_tokens": conclusions * 1_000_000 / total if total else None,
        "interpretation": "Reasoning is included in output. Cached tokens are not uncached cost. Successful tasks and facts are activity proxies, not proof of objective progress. Compare validated useful results and time to proven completion on comparable tasks; billing costs and semantic usefulness are not measured here.",
    }
