from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()


def record_metric(backend, project_id: str, event: dict) -> None:
    try:
        path = Path(backend.reports_dir(project_id)) / "efficiency.jsonl"
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "project_id": project_id, **event}
        with _LOCK:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        LOG.exception("could not persist efficiency metric project=%s", project_id)
