"""Methodology timeline renderer (moved verbatim from the export router).

Output is plain text and byte-stable: the API endpoint, the dispatcher
auto-write and ``board export --format timeline`` all share it.
"""

from __future__ import annotations

import sqlite3

from board.reporting.data import format_export_timestamp, load_project_data


def render_timeline(conn: sqlite3.Connection, project_id: str) -> str:
    data = load_project_data(conn, project_id)
    proj, facts, hints, intents, sources_by_intent = (
        data.project,
        data.facts,
        data.hints,
        data.intents,
        data.sources_by_intent,
    )

    facts_by_id = {f["id"]: f["description"] for f in facts}

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for i in intents:
        src = sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)

        ts = format_export_timestamp(i["created_at"]) or ""
        meta = f"  from: {from_str}"
        if "priority" in i.keys() and i["priority"] is not None:
            meta += f"\n  priority: {i['priority']}"
        if "blocked_by" in i.keys() and i["blocked_by"]:
            meta += f"\n  blocked_by: {i['blocked_by']} (waiting on job)"
        if "assigned_worker" in i.keys() and i["assigned_worker"]:
            meta += f"\n  assigned_to: {i['assigned_worker']}"
        if "assigned_worker_type" in i.keys() and i["assigned_worker_type"]:
            meta += f"\n  worker_type: {i['assigned_worker_type']}"
        if "dispatch_error" in i.keys() and i["dispatch_error"]:
            meta += f"\n  error: {i['dispatch_error']}"
        if "cancel_requested_at" in i.keys() and i["cancel_requested_at"]:
            meta += f"\n  cancellation: {'cancelled' if i['cancelled_at'] else 'stopping'}"
        if i["worker"] and not i["concluded_at"]:
            meta += f"\n  worker: {i['worker']} (in progress)"
        block = f"[{ts}] INTENT DECLARED {i['id']} by {i['creator']}\n{meta}\n  {i['description']}"
        events.append((i["created_at"] or "", order, block))
        order += 1

        if "priority_set_at" in i.keys() and i["priority_set_at"] and "priority_set_by" in i.keys():
            ts2 = format_export_timestamp(i["priority_set_at"]) or ""
            block2 = f"[{ts2}] PRIORITY SET {i['id']} -> {i['priority']} by {i['priority_set_by']}"
            events.append((i["priority_set_at"] or "", order, block2))
            order += 1

        if not i["concluded_at"] or not i["to_fact_id"]:
            continue

        ts = format_export_timestamp(i["concluded_at"]) or ""
        actor = i["worker"] or i["creator"]

        if i["to_fact_id"] == "goal":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {i['id']} from {from_str}"
        else:
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            block = f"[{ts}] INTENT CONCLUDED {i['id']} by {actor}\n  from: {from_str}\n  produced: {i['to_fact_id']}\n  {fact_desc}"

        events.append((i["concluded_at"] or "", order, block))
        order += 1

    for j in getattr(data, "jobs", []) or []:
        ts = format_export_timestamp(j["created_at"]) or ""
        block = f"[{ts}] JOB REGISTERED {j['id']} by {j['creator']}\n  status: {j['status']}\n  {j['description']}"
        events.append((j["created_at"] or "", order, block))
        order += 1

        if j["finished_at"]:
            ts = format_export_timestamp(j["finished_at"]) or ""
            block = f"[{ts}] JOB FINISHED {j['id']} -> {j['status']}\n  produced: {j['fact_id'] or 'none'}\n  {(j['last_output'] or '')[:500]}"
            events.append((j["finished_at"] or "", order, block))
            order += 1

    aborted_at = proj["aborted_at"] if "aborted_at" in proj.keys() else None
    if aborted_at:
        ts = format_export_timestamp(aborted_at) or ""
        aborted_by = proj["aborted_by"] if "aborted_by" in proj.keys() else None
        reason = proj["abort_reason"] if "abort_reason" in proj.keys() else None
        block = f"[{ts}] PROJECT ABORTED by {aborted_by or 'human'}\n  reason: {reason or ''}"
        events.append((aborted_at, order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"
