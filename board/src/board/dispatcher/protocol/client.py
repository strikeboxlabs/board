from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from board.server.models import Intent, ProjectDetail, ProjectSummary, Settings

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class BoardClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def close_thread_session(self) -> None:
        """Release the dedicated heartbeat thread's registered connection pool."""
        session = getattr(self._local, "session", None)
        if session is None:
            return
        with self._sessions_lock:
            self._sessions.pop(threading.get_ident(), None)
        del self._local.session
        session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str, format: str = "yaml") -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": format},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def get_report(self, project_id: str, report_type: str) -> ApiResult:
        """Best-effort report fetch (text body); callers log instead of raising."""
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/report"),
                params={"type": report_type},
                timeout=max(self._timeout, 30.0),
            )
        except requests.RequestException as exc:
            LOG.warning("request failed path=/projects/%s/report error=%s", project_id, exc)
            return ApiResult(status_code=0, text=str(exc))
        return ApiResult(status_code=response.status_code, text=response.text)

    def get_asset_map(self, project_id: str, format: str) -> ApiResult:
        """Best-effort attack-surface map fetch; HTML/JSON/MD rendered server-side."""
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/asset-map"),
                params={"format": format},
                timeout=max(self._timeout, 30.0),
            )
        except requests.RequestException as exc:
            LOG.warning("request failed path=/projects/%s/asset-map error=%s", project_id, exc)
            return ApiResult(status_code=0, text=str(exc))
        return ApiResult(status_code=response.status_code, text=response.text)

    def register_evidence(self, project_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/evidence",
            json=payload,
        )

    def create_finding(self, project_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/findings",
            json=payload,
        )

    def heartbeat(self, project_id: str, intent_id: str, worker: str, *, request_timeout: float | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
            **self._heartbeat_request_options(request_timeout),
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str, lease_id: str | None = None, *, request_timeout: float | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker, **({"lease_id": lease_id} if lease_id is not None else {})},
            **self._heartbeat_request_options(request_timeout),
        )

    def release_reason(self, project_id: str, worker: str, lease_id: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker, **({"lease_id": lease_id} if lease_id is not None else {})},
        )

    def record_reason_wait(self, project_id: str, worker: str, lease_id: str,
                           reason: str, input_signature: str) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/reason/wait",
                                  json={"worker": worker, "lease_id": lease_id,
                                        "reason": reason, "input_signature": input_signature})

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def fail_intent(self, project_id: str, intent_id: str, worker: str | None, error: str) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/intents/{intent_id}/fail",
                                  json={"worker": worker, "error": error[:2000]})

    def acknowledge_intent_cancel(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/intents/{intent_id}/cancelled",
                                  json={"worker": worker})

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={"worker": worker, "description": description},
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker},
        )

    def create_intent(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        creator: str,
        priority: int | None = None,
        blocked_by: str | None = None,
        assigned_worker_type: str | None = None,
        skill_profiles: list[str] | None = None,
    ) -> ApiResult:
        payload: dict[str, Any] = {"from": from_ids, "description": description, "creator": creator, "worker": None}
        if priority is not None:
            payload["priority"] = priority
        if blocked_by:
            payload["blocked_by"] = blocked_by
        if assigned_worker_type is not None:
            payload["assigned_worker_type"] = assigned_worker_type
        if skill_profiles is not None:
            payload["skill_profiles"] = skill_profiles
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json=payload,
        )

    def create_job(self, project_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/jobs",
            json=payload,
        )

    def list_jobs(self, project_id: str) -> list[dict[str, Any]]:
        response = self._session().get(
            self._url(f"/projects/{project_id}/jobs"),
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []

    def progress_job(self, project_id: str, job_id: str, output: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/jobs/{job_id}/progress",
            json={"worker": "dispatcher.jobs", "output": output},
        )

    def finish_job(self, project_id: str, job_id: str, status: str, result: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/jobs/{job_id}/finish",
            json={"worker": "dispatcher.jobs", "status": status, "result": result},
        )

    def acknowledge_suspension(self, project_id: str, suspension_id: str) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/suspension/ack", json={"suspension_id": suspension_id})

    def record_job_stop(self, project_id: str, job_id: str, result: dict) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/jobs/{job_id}/stop-result", json=result)

    @staticmethod
    def _heartbeat_request_options(budget: float | None) -> dict[str, Any]:
        if budget is None:
            return {}
        if budget <= 0:
            raise ValueError("Heartbeat request budget must be positive")
        # Connect/read are individually bounded. HeartbeatLease separately
        # enforces the overall deadline, including DNS and slow response bodies.
        return {"request_timeout": (budget / 4, budget * 3 / 4), "include_response_body": False}

    def _request_json(self, method: str, path: str, json: dict[str, Any], *,
                      request_timeout: tuple[float, float] | None = None,
                      include_response_body: bool = True) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=request_timeout if request_timeout is not None else self._timeout,
                **({"stream": True} if not include_response_body else {}),
            )
        except requests.RequestException as exc:
            if include_response_body:
                LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
                return ApiResult(status_code=0, text=str(exc))
            LOG.warning("heartbeat transport failed path=%s error_type=%s", path, type(exc).__name__)
            return ApiResult(status_code=0)
        if not include_response_body:
            # Renewal needs only the committed HTTP status. Do not wait for or
            # decode a response body before acting on an ownership rejection.
            try:
                return ApiResult(status_code=response.status_code)
            finally:
                close = getattr(response, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception as exc:
                        LOG.warning("heartbeat response cleanup failed error_type=%s", type(exc).__name__)
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
