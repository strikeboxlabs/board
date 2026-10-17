from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from board.dispatcher.protocol.client import ApiResult, BoardClient
from board.dispatcher.runtime.process import ExecProcess


LOG = logging.getLogger(__name__)
HEARTBEAT_FAILURE_GRACE_MULTIPLIER = 2
HEARTBEAT_REQUEST_BUDGET_SECONDS = 1.5
HEARTBEAT_RETRY_DELAY_SECONDS = 0.1
HEARTBEAT_WATCHDOG_TICK_SECONDS = 0.05


@dataclass(slots=True)
class HeartbeatFailure:
    status_code: int | None
    text: str

    @property
    def safe_message(self) -> str:
        """An actionable diagnostic which never includes an HTTP response body."""
        status = f"HTTP {self.status_code}" if self.status_code else "a transport failure"
        if self.status_code in (403, 409):
            return (f"Board rejected heartbeat renewal ({status}); the worker was stopped because ownership "
                    "was no longer accepted.")
        detail = f"last recorded HTTP failure: {status}" if self.status_code else "no timely HTTP confirmation"
        return (f"Board heartbeat renewal could not be confirmed within the existing safety window "
                f"({detail}). The worker was stopped; check Board service and database health.")


@dataclass(slots=True)
class _HeartbeatAttempt:
    started_at: float
    deadline: float
    done: threading.Event
    result: ApiResult | None = None
    completed_at: float | None = None
    timed_out: bool = False


class HeartbeatLease:
    def __init__(
        self,
        heartbeat: Callable[[], ApiResult],
        scope: str,
        worker_name: str,
        interval: int,
        *,
        bounded_heartbeat: Callable[[float], ApiResult] | None = None,
        heartbeat_cleanup: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._heartbeat = heartbeat
        self._scope = scope
        self._worker_name = worker_name
        self._interval = interval
        self._bounded_heartbeat = bounded_heartbeat
        self._heartbeat_cleanup = heartbeat_cleanup
        self._clock = clock
        self._process: ExecProcess | None = None
        self._failure: HeartbeatFailure | None = None
        self._last_success_at = self._clock()
        self._grace_seconds = float(interval * HEARTBEAT_FAILURE_GRACE_MULTIPLIER)
        self._deadline = self._last_success_at + self._grace_seconds
        self._closed = False
        self._last_status_code: int | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._requests: queue.Queue[tuple[_HeartbeatAttempt, float] | None] = queue.Queue()
        self._request_thread: threading.Thread | None = None

    @classmethod
    def for_intent(
        cls,
        client: BoardClient,
        project_id: str,
        intent_id: str,
        worker_name: str,
        interval: int,
    ) -> "HeartbeatLease":
        return cls(
            heartbeat=lambda: client.heartbeat(project_id, intent_id, worker_name),
            scope=f"project={project_id} intent={intent_id}",
            worker_name=worker_name,
            interval=interval,
            bounded_heartbeat=lambda budget: client.heartbeat(project_id, intent_id, worker_name, request_timeout=budget),
            heartbeat_cleanup=getattr(client, "close_thread_session", None),
        )

    @classmethod
    def for_reason(
        cls,
        client: BoardClient,
        project_id: str,
        worker_name: str,
        interval: int,
        lease_id: str | None = None,
    ) -> "HeartbeatLease":
        return cls(
            heartbeat=lambda: client.reason_heartbeat(project_id, worker_name, lease_id) if lease_id is not None else client.reason_heartbeat(project_id, worker_name),
            scope=f"project={project_id} reason",
            worker_name=worker_name,
            interval=interval,
            bounded_heartbeat=lambda budget: client.reason_heartbeat(project_id, worker_name, lease_id, request_timeout=budget),
            heartbeat_cleanup=getattr(client, "close_thread_session", None),
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._closed = True
        self._stop.set()
        self._requests.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    def attach_process(self, process: ExecProcess | None) -> None:
        with self._lock:
            self._process = process
            must_stop = self._closed or self._failure is not None
        if process is not None and must_stop:
            process.kill()

    @property
    def failure(self) -> HeartbeatFailure | None:
        with self._lock:
            return self._failure

    def _begin_attempt(self, budget: float) -> _HeartbeatAttempt:
        started_at = self._clock()
        attempt = _HeartbeatAttempt(started_at, started_at + budget, threading.Event())

        if self._request_thread is None:
            self._request_thread = threading.Thread(target=self._request_loop, daemon=True)
            self._request_thread.start()
        self._requests.put((attempt, budget))
        return attempt

    def _request_loop(self) -> None:
        try:
            while True:
                pending = self._requests.get()
                if pending is None:
                    return
                attempt, budget = pending
                try:
                    if self._stop.is_set():
                        attempt.result = ApiResult(0)
                    else:
                        attempt.result = self._bounded_heartbeat(budget) if self._bounded_heartbeat else self._heartbeat()
                except Exception:
                    # Callback/decoding errors cannot silently end the watchdog;
                    # potentially sensitive exception messages stay private.
                    attempt.result = ApiResult(0)
                finally:
                    attempt.completed_at = self._clock()
                    attempt.done.set()
        finally:
            if self._heartbeat_cleanup is not None:
                try:
                    self._heartbeat_cleanup()
                except Exception as exc:
                    LOG.warning("heartbeat session cleanup failed scope=%s error_type=%s", self._scope, type(exc).__name__)

    def _accept_result(self, attempt: _HeartbeatAttempt) -> bool:
        """Accept only a timely result against the OLD, still-valid window."""
        result = attempt.result or ApiResult(0)
        completed_at = attempt.completed_at if attempt.completed_at is not None else self._clock()
        with self._lock:
            if self._closed or self._failure is not None:
                return False
            expired = self._clock() >= self._deadline or completed_at >= self._deadline
            if not expired and result.status_code not in (403, 409):
                if result.ok and not attempt.timed_out and completed_at <= attempt.deadline:
                    # Receipt time grants no extra freshness. Request latency is
                    # charged to the same conservative interval-based window.
                    self._last_success_at = attempt.started_at
                    self._deadline = attempt.started_at + self._grace_seconds
                    self._last_status_code = None
                    return True
                if not result.ok and result.status_code:
                    self._last_status_code = result.status_code
                return False
            failure_status = result.status_code if not result.ok and result.status_code else self._last_status_code
        self._fail(failure_status, "")
        return False

    def _run(self) -> None:
        next_attempt_at = self._last_success_at + self._interval
        attempt: _HeartbeatAttempt | None = None
        while not self._stop.is_set():
            now = self._clock()
            with self._lock:
                deadline = self._deadline
            if now >= deadline:
                self._fail(self._last_status_code, "")
                return
            if attempt is not None:
                if attempt.done.is_set():
                    accepted = self._accept_result(attempt)
                    if self.failure is not None:
                        return
                    if accepted:
                        next_attempt_at = attempt.started_at + self._interval
                    else:
                        LOG.warning("heartbeat transient failure scope=%s worker=%s status=%s; retrying within existing window",
                                    self._scope, self._worker_name, self._last_status_code)
                        next_attempt_at = self._clock() + min(HEARTBEAT_RETRY_DELAY_SECONDS, self._interval / 4)
                    attempt = None
                    continue
                if now >= attempt.deadline:
                    attempt.timed_out = True
                # Keep at most one request in flight. Its callback may outlive a
                # socket timeout, but cannot block this independent watchdog or
                # renew the lease after the attempt/window has closed.
                wait_for = min(deadline - now, HEARTBEAT_WATCHDOG_TICK_SECONDS)
                if not attempt.timed_out:
                    wait_for = min(wait_for, attempt.deadline - now)
                self._stop.wait(max(0, wait_for))
                continue
            if now < next_attempt_at:
                self._stop.wait(min(next_attempt_at - now, deadline - now))
                continue
            attempt = self._begin_attempt(min(HEARTBEAT_REQUEST_BUDGET_SECONDS, deadline - now))

    def _fail(self, status_code: int | None, text: str) -> None:
        with self._lock:
            if self._closed or self._failure is not None:
                return
            # The first failure is permanent. Never retain a raw response body.
            self._failure = HeartbeatFailure(status_code or None, "")
            process = self._process
        self._stop.set()
        self._requests.put(None)
        LOG.warning(
            "heartbeat failed scope=%s worker=%s status=%s",
            self._scope,
            self._worker_name,
            status_code,
        )
        if process is not None:
            process.kill()
