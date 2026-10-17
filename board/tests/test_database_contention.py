"""Local WAL contention and lease fencing; no workers, models or external calls."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

from board.server import db, services
from board.server.app import app
from board.server.routers import export, intents, projects


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.05)
    db.configure(tmp_path / "contention.db")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/projects", json={"title": "Local contention fixture",
            "origin": "Inert local data", "goal": "Check lease transactions", "bootstrap_enabled": False})
        assert response.status_code == 201
        pid = response.json()["project"]["id"]
        response = client.post(f"/projects/{pid}/intents", json={"from": ["origin"],
            "description": "Inert transaction fixture", "creator": "fixture-owner", "worker": "fixture-owner"})
        assert response.status_code == 201
        yield client, pid, f"/projects/{pid}/intents/i001", db.current_db_path()


@pytest.mark.parametrize("module,hook,route", [
    (db, "hash_db_file", "/projects/{pid}/report"),
    (export, "render_human_report", "/projects/{pid}/report"),
    (export, "build_agent_report", "/projects/{pid}/report?type=agent-json"),
    (export, "render_graph_yaml", "/projects/{pid}/export"),
    (export, "render_timeline", "/projects/{pid}/export?format=timeline"),
    (projects, "project_meta_from_row", "/projects"),
    (projects, "build_intents", "/projects/{pid}"),
])
def test_paused_get_work_does_not_block_heartbeat(fixture, monkeypatch, module, hook, route):
    client, pid, intent_url, _ = fixture
    entered, release = threading.Event(), threading.Event()
    original = getattr(module, hook)

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Fixture rendering was never released"
        return original(*args, **kwargs)

    monkeypatch.setattr(module, hook, paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        rendering = executor.submit(client.get, route.format(pid=pid))
        try:
            assert entered.wait(5), "GET did not reach the slow work"
            response = client.post(intent_url + "/heartbeat", json={"worker": "fixture-owner"})
            assert response.status_code == 200, response.text
        finally:
            release.set()
        assert rendering.result(timeout=5).status_code == 200


def test_expired_claim_is_committed_before_report_hashing(fixture, monkeypatch):
    client, pid, _, path = fixture
    with db.get_conn() as conn:
        conn.execute("UPDATE intents SET last_heartbeat_at='2000-01-01T00:00:00Z'")
    entered, release = threading.Event(), threading.Event()

    def paused_hash():
        entered.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr(db, "hash_db_file", paused_hash)
    with ThreadPoolExecutor(max_workers=1) as executor:
        rendering = executor.submit(client.get, f"/projects/{pid}/report")
        try:
            assert entered.wait(5)
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as reader:
                assert reader.execute("SELECT worker FROM intents WHERE id='i001'").fetchone()[0] is None
        finally:
            release.set()
        assert rendering.result(timeout=5).status_code == 200


def test_read_snapshot_is_consistent_and_rejects_writes(fixture):
    _, pid, _, _ = fixture
    with db.get_read_conn() as snapshot:
        before = snapshot.execute("SELECT title FROM projects WHERE id=?", (pid,)).fetchone()[0]
        with db.get_conn() as writer:
            writer.execute("UPDATE projects SET title='Updated fixture' WHERE id=?", (pid,))
        assert snapshot.execute("SELECT title FROM projects WHERE id=?", (pid,)).fetchone()[0] == before
        with pytest.raises(sqlite3.OperationalError) as failure:
            snapshot.execute("UPDATE projects SET title='Forbidden write' WHERE id=?", (pid,))
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    with db.get_conn() as conn:
        assert conn.execute("SELECT title FROM projects WHERE id=?", (pid,)).fetchone()[0] == "Updated fixture"


def test_real_writer_contention_returns_retryable_503_without_claim_change(fixture):
    client, _, intent_url, path = fixture
    holder = sqlite3.connect(path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        response = client.post(intent_url + "/heartbeat", json={"worker": "fixture-owner"})
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.json() == {"detail": "Board database is busy; retry shortly.", "code": "database_busy"}
        assert "sqlite" not in response.text and str(path) not in response.text
    finally:
        holder.rollback()
        holder.close()
    assert client.post(intent_url + "/heartbeat", json={"worker": "fixture-owner"}).status_code == 200


def busy_error(path):
    holder, contender = sqlite3.connect(path), sqlite3.connect(path, timeout=0)
    try:
        holder.execute("BEGIN IMMEDIATE")
        try:
            contender.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            return exc
        raise AssertionError("Expected a real SQLITE_BUSY exception")
    finally:
        holder.rollback()
        holder.close()
        contender.close()


def test_busy_error_after_mutation_rolls_back_before_503(fixture, monkeypatch):
    client, _, intent_url, path = fixture
    with db.get_conn() as conn:
        original = tuple(conn.execute("SELECT worker,last_heartbeat_at FROM intents WHERE id='i001'").fetchone())
    error = busy_error(path)
    monkeypatch.setattr(intents, "utcnow", lambda: "2030-01-01T00:00:00Z")

    def failed_projection(*_args):
        raise error

    monkeypatch.setattr(intents, "intent_to_model", failed_projection)
    response = client.post(intent_url + "/heartbeat", json={"worker": "fixture-owner"})
    assert response.status_code == 503
    with db.get_conn() as conn:
        current = tuple(conn.execute("SELECT worker,last_heartbeat_at FROM intents WHERE id='i001'").fetchone())
    assert current == original


def test_ownership_conflicts_stay_409_and_unrelated_sql_errors_stay_errors(fixture, monkeypatch):
    client, _, intent_url, _ = fixture
    conflict = client.post(intent_url + "/heartbeat", json={"worker": "other-fixture-worker"})
    assert conflict.status_code == 409 and "retry-after" not in conflict.headers

    def invalid_query(*_args):
        raise sqlite3.OperationalError("no such table: inert_fixture_missing")

    monkeypatch.setattr(intents, "intent_to_model", invalid_query)
    failure = client.post(intent_url + "/heartbeat", json={"worker": "fixture-owner"})
    assert failure.status_code == 500 and "retry-after" not in failure.headers
    assert "inert_fixture_missing" not in failure.text


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                                  sqlite3.SQLITE_BUSY | (2 << 8), sqlite3.SQLITE_LOCKED | (1 << 8)])
def test_only_numeric_primary_or_extended_contention_codes_are_retryable(code):
    error = sqlite3.OperationalError("Details must not control classification")
    error.sqlite_errorcode = code
    assert db.is_contention_error(error)
    assert not db.is_contention_error(sqlite3.OperationalError("database is locked"))
    error.sqlite_errorcode = sqlite3.SQLITE_ERROR
    assert not db.is_contention_error(error)


@pytest.mark.parametrize("operation", ["claim", "heartbeat", "release"])
def test_reason_expiry_clock_is_sampled_after_waiting_for_writer(fixture, monkeypatch, operation):
    client, pid, _, path = fixture
    claimed = client.post(f"/projects/{pid}/reason/claim", json={"worker": "fixture-owner", "trigger": "fixture"})
    assert claimed.status_code == 200
    lease_id = claimed.json()["reason"]["lease_id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET reason_last_heartbeat_at='2030-01-01T00:00:00Z' WHERE id=?", (pid,))
    clock = {"now": "2030-01-01T00:00:10Z"}
    monkeypatch.setattr(services, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(projects, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 3.0)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    attempting = threading.Event()
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)

        def trace(sql):
            normalized = sql.lstrip().upper()
            if normalized.startswith(("BEGIN IMMEDIATE", "UPDATE PROJECTS")):
                attempting.set()

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(db.sqlite3, "connect", traced_connect)
    body = {"worker": "new-fixture-owner", "trigger": "fixture"} if operation == "claim" else {
        "worker": "fixture-owner", "lease_id": lease_id}
    with ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(client.post, f"/projects/{pid}/reason/{operation}", json=body)
        try:
            assert attempting.wait(5)
            clock["now"] = "2030-01-01T00:00:20Z"
        finally:
            holder.rollback()
            holder.close()
        response = request.result(timeout=5)
    if operation == "heartbeat":
        assert response.status_code == 409, response.text
    else:
        assert response.status_code == 200, response.text
        reason = response.json()["reason"]
        assert (reason["worker"] == "new-fixture-owner") if operation == "claim" else reason is None
