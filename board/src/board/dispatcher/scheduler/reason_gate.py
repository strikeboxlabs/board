"""Persist retry budgets per graph revision so restarts do not restart a retry storm."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from pathlib import Path

LOG = logging.getLogger(__name__)


def graph_signature(project, config) -> str:
    # Ignore leases and heartbeats: they do not make a failed decision useful again.
    payload = {
        "facts": [f.model_dump() for f in project.facts],
        "hints": [h.model_dump() for h in project.hints],
        "intents": [(i.id, i.to, i.description, i.priority, i.blocked_by, i.assigned_worker, i.excluded) for i in project.intents],
        "jobs": [(j.id, j.status) for j in project.jobs],
        "policy": config.model_dump(),
        "execution_notes": project.project.execution_notes,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ReasonGate:
    def __init__(self, backend, config):
        self.backend = backend
        self.config = config
        self.states: dict[str, dict] = {}

    def _path(self, project_id: str) -> Path:
        return Path(self.backend.reports_dir(project_id)) / "planner-state.json"

    def state(self, project_id: str) -> dict:
        if project_id not in self.states:
            try:
                state = json.loads(self._path(project_id).read_text())
                if not isinstance(state, dict):
                    raise ValueError("invalid planner state")
                int(state.get("attempts", 0))
                float(state.get("next_at", 0))
                self.states[project_id] = state
            except FileNotFoundError:
                self.states[project_id] = {}
            except (OSError, ValueError, TypeError):
                LOG.exception("cannot load planner state project=%s; blocking planning until repaired", project_id)
                self.states[project_id] = {"unreadable": True}
        return self.states[project_id]

    def allows(self, project_id: str, signature: str) -> bool:
        state = self.state(project_id)
        if state.get("unreadable") or time.time() < state.get("next_at", 0):
            return False
        if state.get("signature") != signature:
            return True
        return not state.get("succeeded") and state.get("attempts", 0) < self.config.max_attempts_per_graph

    def _save(self, project_id: str, state: dict) -> None:
        path = self._path(project_id)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(state, sort_keys=True) + "\n")
        temp.replace(path)
        self.states[project_id] = state

    def started(self, project_id: str, signature: str) -> None:
        old = self.state(project_id)
        attempts = old.get("attempts", 0) if old.get("signature") == signature else 0
        self._save(project_id, {"signature": signature, "attempts": attempts + 1,
                              "next_at": time.time() + self.config.retry_backoff_seconds,
                              "succeeded": False})

    def finished(self, project_id: str, outcome: str, checkpoint: dict | None = None) -> None:
        state = dict(self.state(project_id))
        if not state or state.get("unreadable"):
            return
        succeeded = outcome == "success"
        state["succeeded"] = succeeded
        state["last_outcome"] = outcome
        if succeeded and checkpoint is not None:
            state["checkpoint"] = checkpoint
        delay = self.config.cooldown_seconds if succeeded else min(900, self.config.retry_backoff_seconds * 2 ** min(state.get("attempts", 1) - 1, 10))
        state["next_at"] = time.time() + delay
        self._save(project_id, state)
        if not succeeded and state["attempts"] >= self.config.max_attempts_per_graph:
            LOG.error("planner retry budget exhausted project=%s attempts=%s; waiting for graph/policy change", project_id, state["attempts"])
