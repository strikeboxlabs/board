from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "board" / "board.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT,
    abort_reason TEXT,
    aborted_at TEXT,
    aborted_by TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    priority INTEGER NOT NULL DEFAULT 50,
    blocked_by TEXT,
    assigned_worker TEXT,
    assigned_worker_type TEXT,
    skill_profiles TEXT NOT NULL DEFAULT '[]',
    dispatch_error TEXT,
    dispatch_error_at TEXT,
    dispatch_error_worker TEXT,
    cancel_requested_at TEXT,
    cancelled_at TEXT,
    cancelled_by TEXT,
    priority_set_by TEXT,
    priority_set_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','hit','done','exhausted','failed','killed')),
    check_command TEXT,
    stop_command TEXT,
    budget_seconds INTEGER,
    creator TEXT NOT NULL,
    last_check_at TEXT,
    last_output TEXT,
    fact_id TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    intent_id TEXT,
    fact_id TEXT,
    path TEXT NOT NULL,
    kind TEXT,
    sha256 TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    description TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fact_id TEXT NOT NULL,
    intent_id TEXT,
    title TEXT NOT NULL,
    category TEXT,
    severity TEXT CHECK (severity IN ('critical','high','medium','low','info','negative')),
    status TEXT CHECK (status IN ('open','confirmed','mitigated','false_positive','needs_retest','blocked')),
    affected_target TEXT,
    description TEXT NOT NULL,
    recommendation TEXT,
    evidence_ids TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_project_columns(conn)
        _ensure_intent_columns(conn)
        _ensure_job_columns(conn)
        for column in ("stop_state", "stop_error"):
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if column not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        _ensure_planner_wait_invalidation(conn)
        # Bring projects stopped by older clients under the same acknowledgement
        # barrier; their detached jobs must not survive a nominal suspension.
        conn.execute("""UPDATE projects SET suspension_id = lower(hex(randomblob(16))), suspension_ack_id = NULL
                        WHERE status = 'stopped' AND suspension_id IS NULL""")
        conn.execute("""UPDATE jobs SET status = 'killed', finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                        stop_state = CASE WHEN trim(coalesce(stop_command, '')) != '' THEN 'pending' ELSE 'manual' END
                        WHERE status = 'running' AND project_id IN (SELECT id FROM projects WHERE status = 'stopped')""")


def backup_existing_db() -> None:
    """Best-effort timestamped copy of the database at server startup.

    The blackboard DB is the surviving record of every run; a corrupted
    file would lose everything, so keep one timestamped backup per serve
    start next to the live file. Called from ``board serve`` only — CLI
    commands must not spam backups.
    """
    if _db_path is None or not _db_path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = _db_path.with_name(f"{_db_path.name}.backup-{stamp}")
    try:
        shutil.copy2(_db_path, backup)
    except OSError:
        return


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )
    for column in ("abort_reason", "aborted_at", "aborted_by", "suspension_id", "suspension_ack_id",
                   "reason_lease_id", "planner_wait", "completion_outcome"):
        if column not in columns:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {column} TEXT")

    if "execution_notes" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN execution_notes TEXT NOT NULL DEFAULT ''")


def _ensure_planner_wait_invalidation(conn: sqlite3.Connection) -> None:
    """New evidence/work invalidates a decision; heartbeat-only updates do not."""
    updates = {
        "facts": "description",
        "hints": "content",
        "intents": "to_fact_id, description, priority, blocked_by, assigned_worker, assigned_worker_type, skill_profiles, excluded, dispatch_error, cancel_requested_at, cancelled_at",
        "jobs": "status",
    }
    for table, columns in updates.items():
        for event, reference in (("INSERT", "NEW"), ("DELETE", "OLD"), (f"UPDATE OF {columns}", "NEW")):
            suffix = event.split()[0].lower()
            predicate = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in columns.split(", "))
            when = f"WHEN {predicate}" if suffix == "update" else ""
            # Refresh existing definitions when upgrading an earlier install.
            conn.execute(f"DROP TRIGGER IF EXISTS clear_planner_wait_{table}_{suffix}")
            conn.execute(f"""CREATE TRIGGER clear_planner_wait_{table}_{suffix}
                AFTER {event} ON {table} {when} BEGIN
                UPDATE projects SET planner_wait = NULL WHERE id = {reference}.project_id;
                END""")
    conn.execute("DROP TRIGGER IF EXISTS clear_planner_wait_policy")
    conn.execute("""CREATE TRIGGER clear_planner_wait_policy
        AFTER UPDATE OF status, execution_notes ON projects
        WHEN OLD.status IS NOT NEW.status OR OLD.execution_notes IS NOT NEW.execution_notes BEGIN
        UPDATE projects SET planner_wait = NULL WHERE id = NEW.id;
        END""")


def _ensure_job_columns(conn: sqlite3.Connection) -> None:
    """Upgrade pre-kill jobs tables: stop_command column + 'killed' status.

    SQLite cannot ALTER a CHECK constraint, so when the live jobs table
    predates the 'killed' status it is rebuilt (create-copy-drop-rename).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if row is None:
        return
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    create_sql = row["sql"] or ""
    needs_rebuild = "'killed'" not in create_sql
    if not needs_rebuild and "stop_command" in columns:
        return
    conn.executescript(
        """
        CREATE TABLE jobs_new (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','hit','done','exhausted','failed','killed')),
            check_command TEXT,
            stop_command TEXT,
            budget_seconds INTEGER,
            creator TEXT NOT NULL,
            last_check_at TEXT,
            last_output TEXT,
            fact_id TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            PRIMARY KEY (id, project_id)
        );
        INSERT INTO jobs_new (id, project_id, description, status, check_command, budget_seconds, creator, last_check_at, last_output, fact_id, created_at, finished_at)
            SELECT id, project_id, description, status, check_command, budget_seconds, creator, last_check_at, last_output, fact_id, created_at, finished_at FROM jobs;
        DROP TABLE jobs;
        ALTER TABLE jobs_new RENAME TO jobs;
        """
    )


def _ensure_intent_columns(conn: sqlite3.Connection) -> None:
    """Upgrade intent scheduling and operator exclusion columns."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)")}
    if "excluded" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
    if "priority" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN priority INTEGER NOT NULL DEFAULT 50")
    if "blocked_by" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN blocked_by TEXT")
    if "assigned_worker" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN assigned_worker TEXT")
    if "assigned_worker_type" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN assigned_worker_type TEXT")
    if "skill_profiles" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN skill_profiles TEXT NOT NULL DEFAULT '[]'")
    if "priority_set_by" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN priority_set_by TEXT")
    if "priority_set_at" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN priority_set_at TEXT")
    for column in ("dispatch_error", "dispatch_error_at", "dispatch_error_worker", "cancel_requested_at", "cancelled_at", "cancelled_by"):
        if column not in columns:
            conn.execute(f"ALTER TABLE intents ADD COLUMN {column} TEXT")


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def current_db_path() -> Path | None:
    return _db_path


def hash_db_file() -> str | None:
    """Best-effort sha256 of the database file, for report integrity headers."""
    if _db_path is None or not _db_path.exists():
        return None
    digest = hashlib.sha256()
    try:
        with _db_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()
