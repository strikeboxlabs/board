"""Stable planner inputs shared by scheduling and durable decision writes."""
from __future__ import annotations

import hashlib
import json


def planner_inputs(project) -> dict:
    # Worker leases and progress heartbeats are deliberately excluded.
    return {
        "facts": [f.model_dump() for f in project.facts],
        "hints": [h.model_dump() for h in project.hints],
        "intents": [(i.id, i.to, i.description, i.priority, i.blocked_by,
                     i.assigned_worker, i.assigned_worker_type, i.skill_profiles,
                     i.excluded, i.dispatch_error, i.cancel_requested_at, i.cancelled_at)
                    for i in project.intents],
        "jobs": [(j.id, j.status) for j in project.jobs],
        "execution_notes": project.project.execution_notes,
    }


def planner_input_signature(project) -> str:
    return hashlib.sha256(json.dumps(planner_inputs(project), sort_keys=True).encode()).hexdigest()
