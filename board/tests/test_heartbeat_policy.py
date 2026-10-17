"""Bounded heartbeat policy tests with synthetic clocks/callbacks only."""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest
import requests

from board.dispatcher.protocol.client import ApiResult, BoardClient
from board.dispatcher.runtime.heartbeat import (
    HEARTBEAT_REQUEST_BUDGET_SECONDS, HeartbeatFailure, HeartbeatLease, _HeartbeatAttempt,
)
from board.server import db


@dataclass
class Clock:
    now: float = 0

    def __call__(self):
        return self.now


class Process:
    def __init__(self):
        self.killed = threading.Event()
        self.kill_count = 0

    def kill(self):
        self.kill_count += 1
        self.killed.set()


def completed_attempt(started, completed, status, *, budget=1):
    attempt = _HeartbeatAttempt(started, started + budget, threading.Event(),
                                ApiResult(status, text="RAW_RESPONSE_SECRET"), completed)
    attempt.done.set()
    return attempt


def test_transient_failure_and_prompt_success_share_existing_window():
    clock = Clock()
    lease = HeartbeatLease(lambda: ApiResult(200), "fixture", "worker", 3, clock=clock)
    clock.now = 3.8
    assert not lease._accept_result(completed_attempt(3, 3.8, 503))
    assert lease.failure is None and lease._deadline == 6
    clock.now = 4.2
    assert lease._accept_result(completed_attempt(4, 4.2, 200))
    assert lease._deadline == 10  # request-start + 6, not response-receipt + 6
    lease.stop()


def test_late_success_cannot_replace_old_deadline_before_checking_it():
    clock = Clock()
    lease = HeartbeatLease(lambda: ApiResult(200), "fixture", "worker", 3, clock=clock)
    process = Process()
    lease.attach_process(process)
    clock.now = 8.1
    assert not lease._accept_result(completed_attempt(3, 8.1, 200, budget=10))
    assert lease.failure is not None and lease._deadline == 6
    assert process.kill_count == 1
    lease.stop()


def test_success_outside_attempt_budget_does_not_renew_or_extend_window():
    clock = Clock()
    lease = HeartbeatLease(lambda: ApiResult(200), "fixture", "worker", 3, clock=clock)
    clock.now = 4.1
    assert not lease._accept_result(completed_attempt(3, 4.1, 200))
    assert lease.failure is None and lease._deadline == 6
    lease.stop()


@pytest.mark.parametrize("status", [403, 409])
def test_explicit_rejection_stops_immediately_and_failure_is_permanent(status):
    clock = Clock()
    lease = HeartbeatLease(lambda: ApiResult(200), "fixture", "worker", 3, clock=clock)
    process = Process()
    lease.attach_process(process)
    clock.now = 3.1
    assert not lease._accept_result(completed_attempt(3, 3.1, status))
    first = lease.failure
    assert first.status_code == status and process.kill_count == 1
    lease._fail(500, "LATER_RAW_BODY")
    assert lease.failure is first and "RAW" not in first.safe_message and first.text == ""
    assert not lease._accept_result(completed_attempt(3.2, 3.3, 200))
    assert lease._deadline == 6 and process.kill_count == 1
    lease.stop()


def test_stopped_lease_ignores_success_and_kills_late_process_attachment():
    clock = Clock()
    lease = HeartbeatLease(lambda: ApiResult(200), "fixture", "worker", 3, clock=clock)
    lease.stop()
    clock.now = 3.1
    assert not lease._accept_result(completed_attempt(3, 3.1, 200))
    process = Process()
    lease.attach_process(process)
    assert process.kill_count == 1 and lease._deadline == 6


def test_watchdog_stops_despite_blocked_callback_and_ignores_its_late_success():
    release = threading.Event()
    entered = threading.Event()
    cleaned = threading.Event()

    def callback():
        entered.set()
        release.wait(2)
        return ApiResult(200)

    lease = HeartbeatLease(callback, "fixture", "worker", 0.1, heartbeat_cleanup=cleaned.set)
    process = Process()
    lease.attach_process(process)
    lease.start()
    try:
        assert entered.wait(1)
        assert process.killed.wait(1)  # request callback is still blocked
        first = lease.failure
        assert first is not None and not release.is_set()
        late_process = Process()
        lease.attach_process(late_process)
        assert late_process.kill_count == 1
        release.set()
        assert cleaned.wait(1)
        assert lease.failure is first
    finally:
        release.set()
        lease.stop()


def test_callback_exception_remains_visible_and_never_leaks_its_message(caplog):
    def callback():
        raise ValueError("CALLBACK_SECRET")

    lease = HeartbeatLease(callback, "fixture", "worker", 0.05)
    process = Process()
    lease.attach_process(process)
    lease.start()
    try:
        assert process.killed.wait(1)
        assert lease.failure is not None
        assert "CALLBACK_SECRET" not in lease.failure.safe_message + caplog.text
    finally:
        lease.stop()


@pytest.mark.parametrize("close_raises", [False, True])
def test_bounded_transport_preserves_status_without_reading_raw_body(close_raises, caplog):
    calls = []
    closed = []

    class Response:
        status_code = 409

        @property
        def text(self):
            pytest.fail("Raw heartbeat body was accessed")

        def close(self):
            closed.append(True)
            if close_raises:
                raise RuntimeError("CLOSE_SECRET")

    class Session:
        def request(self, *_args, **kwargs):
            calls.append(kwargs)
            return Response()

    client = BoardClient("http://unused")
    client._local.session = Session()
    result = client.heartbeat("fixture", "i001", "worker", request_timeout=0.4)
    assert result.status_code == 409 and result.text == "" and result.data is None
    assert calls[0]["timeout"] == pytest.approx((0.1, 0.3))
    assert calls[0]["stream"] is True and closed == [True]
    assert "CLOSE_SECRET" not in caplog.text


def test_heartbeat_exception_uses_short_budget_and_sanitized_diagnostic(caplog):
    class Session:
        def request(self, *_args, **kwargs):
            assert sum(kwargs["timeout"]) == pytest.approx(0.4)
            raise requests.ConnectionError("TRANSPORT_SECRET")

    client = BoardClient("http://unused")
    client._local.session = Session()
    result = client.reason_heartbeat("fixture", "worker", "lease", request_timeout=0.4)
    assert result.status_code == 0 and result.text == ""
    assert "TRANSPORT_SECRET" not in caplog.text


def test_prompt_retry_uses_one_session_and_releases_its_registration(monkeypatch):
    sessions = []
    closed = threading.Event()

    class Session:
        def __init__(self):
            self.calls = 0
            sessions.append(self)

        def mount(self, *_args):
            pass

        def request(self, *_args, **kwargs):
            assert sum(kwargs["timeout"]) <= 1
            self.calls += 1
            status = [503, 200, 409][self.calls - 1]
            return type("Response", (), {"status_code": status})()

        def close(self):
            closed.set()

    monkeypatch.setattr(requests, "Session", Session)
    client = BoardClient("http://unused")
    lease = HeartbeatLease.for_intent(client, "fixture", "i001", "worker", 0.2)
    process = Process()
    lease.attach_process(process)
    lease.start()
    try:
        assert process.killed.wait(2)
        assert lease.failure.status_code == 409  # recovered from first 503
        assert closed.wait(1)
        assert len(sessions) == 1 and sessions[0].calls == 3
        assert client._sessions == {}
    finally:
        lease.stop()
        client.close()


def test_failure_message_never_uses_supplied_response_body():
    failure = HeartbeatFailure(503, "RAW_RESPONSE_SECRET")
    assert "HTTP 503" in failure.safe_message
    assert "database health" in failure.safe_message
    assert "RAW_RESPONSE_SECRET" not in failure.safe_message


def test_known_http_failure_survives_later_transport_timeout():
    clock = Clock()
    lease = HeartbeatLease(lambda: ApiResult(200), "fixture", "worker", 3, clock=clock)
    clock.now = 3.8
    assert not lease._accept_result(completed_attempt(3, 3.8, 503))
    clock.now = 4.2
    assert not lease._accept_result(completed_attempt(4, 4.2, 0))
    assert lease.failure is None and lease._deadline == 6
    clock.now = 6.1
    assert not lease._accept_result(completed_attempt(5, 6.1, 0, budget=2))
    assert lease.failure.status_code == 503
    assert "last recorded HTTP failure: HTTP 503" in lease.failure.safe_message
    lease.stop()


def test_renewal_can_receive_busy_response_after_server_lock_wait():
    class BusyHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            time.sleep(db.BUSY_TIMEOUT_SECONDS)
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    options = BoardClient._heartbeat_request_options(HEARTBEAT_REQUEST_BUDGET_SECONDS)
    assert options["request_timeout"][1] > db.BUSY_TIMEOUT_SECONDS
    server = ThreadingHTTPServer(("127.0.0.1", 0), BusyHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = BoardClient(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        result = client.heartbeat("fixture", "i001", "worker",
                                  request_timeout=HEARTBEAT_REQUEST_BUDGET_SECONDS)
        assert result.status_code == 503 and result.text == ""
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
