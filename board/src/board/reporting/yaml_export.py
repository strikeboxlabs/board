"""Graph YAML snapshot renderer (moved verbatim from the export router)."""

from __future__ import annotations

import json
import sqlite3

import yaml

from board.reporting.data import format_export_timestamp, load_project_data


def render_graph_yaml(conn: sqlite3.Connection, project_id: str) -> str:
    data = load_project_data(conn, project_id)
    proj, facts, hints, intents, sources_by_intent = (
        data.project,
        data.facts,
        data.hints,
        data.intents,
        data.sources_by_intent,
    )

    origin_desc = ""
    goal_desc = ""
    for f in facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
        elif f["id"] == "goal":
            goal_desc = f["description"]

    graph: dict = {
        "project": {
            "title": proj["title"],
            "origin": origin_desc,
            "goal": goal_desc,
            "bootstrap_enabled": bool(proj["bootstrap_enabled"]),
        }
    }

    if proj["execution_notes"]:
        graph["project"]["execution_notes"] = proj["execution_notes"]

    if hints:
        graph["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    graph["facts"] = [{"id": f["id"], "description": f["description"]} for f in facts]

    intent_list = []
    for i in intents:
        entry: dict = {
            "from": sources_by_intent.get(i["id"], []),
            "to": i["to_fact_id"],
            "description": i["description"],
            "creator": i["creator"],
            "worker": i["worker"],
            "created_at": format_export_timestamp(i["created_at"]),
            "concluded_at": format_export_timestamp(i["concluded_at"]),
        }
        if i["excluded"]:
            entry["id"] = i["id"]
            entry["excluded"] = True
        priority = i["priority"] if "priority" in i.keys() else None
        if priority is not None:
            entry["priority"] = priority
        blocked_by = i["blocked_by"] if "blocked_by" in i.keys() else None
        if blocked_by:
            entry["blocked_by"] = blocked_by
        assigned_worker = i["assigned_worker"] if "assigned_worker" in i.keys() else None
        if assigned_worker:
            entry["assigned_worker"] = assigned_worker
        assigned_worker_type = i["assigned_worker_type"] if "assigned_worker_type" in i.keys() else None
        if assigned_worker_type:
            entry["assigned_worker_type"] = assigned_worker_type
        if "skill_profiles" in i.keys() and i["skill_profiles"] != "[]":
            entry["skill_profiles"] = json.loads(i["skill_profiles"])
        for column in ("dispatch_error", "dispatch_error_at", "dispatch_error_worker", "cancel_requested_at", "cancelled_at", "cancelled_by"):
            if column in i.keys() and i[column]:
                entry[column] = i[column]
        if "priority_set_by" in i.keys() and i["priority_set_by"]:
            entry["priority_set_by"] = i["priority_set_by"]
        intent_list.append(entry)

    if intent_list:
        graph["intents"] = intent_list

    jobs = data.jobs
    if jobs:
        job_entries = []
        for j in jobs:
            entry = {
                "id": j["id"],
                "description": j["description"],
                "status": j["status"],
                "budget_seconds": j["budget_seconds"],
                "fact": j["fact_id"],
                "last_output": j["last_output"],
                "created_at": format_export_timestamp(j["created_at"]),
                "finished_at": format_export_timestamp(j["finished_at"]),
            }
            last_check = j["last_check_at"] if "last_check_at" in j.keys() else None
            if last_check:
                entry["last_check_at"] = format_export_timestamp(last_check)
                end_ref = j["finished_at"] or last_check
                try:
                    from datetime import datetime
                    started = datetime.strptime(j["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                    ended = datetime.strptime(end_ref, "%Y-%m-%dT%H:%M:%SZ")
                    entry["elapsed_seconds"] = int((ended - started).total_seconds())
                except ValueError:
                    pass
            job_entries.append(entry)
        graph["jobs"] = job_entries

    return yaml.dump(graph, allow_unicode=True, default_flow_style=False, sort_keys=False)
