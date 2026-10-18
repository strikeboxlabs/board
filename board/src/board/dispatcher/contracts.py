from __future__ import annotations

from collections.abc import Collection
from typing import Any

from board.skill_profiles import validate_skill_profile_ids

from board.dispatcher.output_parser import extract_json_object

ALLOWED_FINDING_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info", "negative"})
ALLOWED_FINDING_STATUSES = frozenset({"open", "confirmed", "mitigated", "false_positive", "needs_retest", "blocked"})
MAX_FINDINGS_PER_FACT = 20
MAX_JOBS_PER_CONCLUDE = 10


class UnavailableSkillProfileError(ValueError):
    """A model selected IDs outside this call's advertised profile choices."""

    def __init__(self, intent_index: int, requested: list[str], allowed: Collection[str]):
        self.intent_index = intent_index
        self.unavailable_ids = [profile_id for profile_id in requested if profile_id not in allowed]
        self.allowed_ids = sorted(allowed)
        valid = ", ".join(self.allowed_ids) or "none (omit skill_profiles)"
        super().__init__(f"intent at index {intent_index} requests unavailable skill profile IDs: "
                         f"{', '.join(self.unavailable_ids)}. Valid profile IDs: {valid}.")


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or set(payload) != {"fact", "complete"}:
        return False
    return _is_dict(payload.get("fact")) and _is_dict(payload.get("complete"))


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact"))


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and set(payload) in (
        {"description"},
        {"description", "findings"},
        {"description", "jobs"},
        {"description", "findings", "jobs"},
    )


def validate_optional_findings(value: Any) -> list[dict[str, Any]]:
    """Validate the optional structured-findings array a worker may return with a fact.

    Each entry needs at least a title; severity defaults to ``info``. An explicit
    status is preserved; otherwise the API defaults to ``open``. Entries are
    normalised so the dispatcher can POST them straight to the findings API.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("findings must be an array")
    if len(value) > MAX_FINDINGS_PER_FACT:
        raise ValueError(f"findings must not exceed {MAX_FINDINGS_PER_FACT} entries")
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"findings[{index}] must be an object")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"findings[{index}].title is required")
        severity = item.get("severity", "info")
        if severity not in ALLOWED_FINDING_SEVERITIES:
            raise ValueError(
                f"findings[{index}].severity must be one of: {', '.join(sorted(ALLOWED_FINDING_SEVERITIES))}"
            )
        entry: dict[str, Any] = {"title": title.strip(), "severity": severity}
        if "status" in item:
            status = item["status"]
            if not isinstance(status, str) or status not in ALLOWED_FINDING_STATUSES:
                raise ValueError(
                    f"findings[{index}].status must be one of: {', '.join(sorted(ALLOWED_FINDING_STATUSES))}"
                )
            entry["status"] = status
        for key in ("category", "affected_target", "description", "recommendation"):
            text = item.get(key)
            if isinstance(text, str) and text.strip():
                entry[key] = text.strip()
        entry.setdefault("description", title.strip())
        cleaned.append(entry)
    return cleaned


def validate_optional_jobs(value: Any) -> list[dict[str, Any]]:
    """Validate the optional background-jobs array a worker may register at conclude.

    Each job: ``description`` (required), ``check_command`` (required — run by
    the dispatcher every poll; must print a line starting with HIT or DONE
    exactly once when the job produces its result), ``budget_seconds``
    (optional wall-clock cap). Entries are normalised for the jobs API.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("jobs must be an array")
    if len(value) > MAX_JOBS_PER_CONCLUDE:
        raise ValueError(f"jobs must not exceed {MAX_JOBS_PER_CONCLUDE} entries")
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"jobs[{index}] must be an object")
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"jobs[{index}].description is required")
        check_command = item.get("check_command")
        if not isinstance(check_command, str) or not check_command.strip():
            raise ValueError(f"jobs[{index}].check_command is required")
        stop_command = item.get("stop_command")
        if stop_command is not None and (not isinstance(stop_command, str) or not stop_command.strip()):
            raise ValueError(f"jobs[{index}].stop_command must be a non-empty string when provided")
        budget = item.get("budget_seconds")
        if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool) or not 60 <= budget <= 2_592_000):
            raise ValueError(f"jobs[{index}].budget_seconds must be an integer between 60 and 2592000")
        entry: dict[str, Any] = {
            "description": description.strip()[:2000],
            "check_command": check_command.strip()[:4000],
        }
        if stop_command and stop_command.strip():
            entry["stop_command"] = stop_command.strip()[:4000]
        if budget is not None:
            entry["budget_seconds"] = budget
        cleaned.append(entry)
    return cleaned


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
    *, allowed_skill_profiles: Collection[str] | None = None,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    complete = data.get("complete")
    intents = data.get("intents")
    # backward compat: accept singular "intent" key from LLMs
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return "complete", complete
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {i}")
            if "skill_profiles" in intent:
                requested = validate_skill_profile_ids(intent["skill_profiles"])
                if allowed_skill_profiles is not None and any(profile_id not in allowed_skill_profiles for profile_id in requested):
                    raise UnavailableSkillProfileError(i, requested, allowed_skill_profiles)
            priority = intent.get("priority")
            if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 100):
                raise ValueError(f"intent at index {i}: priority must be an integer between 1 and 100")
            blocked_by = intent.get("blocked_by")
            if blocked_by is not None and (not isinstance(blocked_by, str) or not blocked_by.strip()):
                raise ValueError(f"intent at index {i}: blocked_by must be a non-empty job id")
        if not intents and open_intents_empty:
            raise ValueError("intents must not be empty when open_intents is empty")
        intents = intents[:max_intents]
        if not intents:
            return "noop", None
        return "intents", intents
    wait = data.get("wait")
    if isinstance(wait, str) and wait.strip():
        return "wait", {"reason": wait.strip()}
    if open_intents_empty:
        raise ValueError("intents or an explicit wait reason is required when open_intents is empty")
    return "noop", None


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        raise ValueError("complete is required")
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    return "complete", result


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    extra_keys = set(data) - {"fact", "complete"}
    if extra_keys:
        raise ValueError("unexpected keys in conclude payload")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    return "fact", fact_description.strip()


def validate_explore_payload(payload: dict[str, Any]) -> tuple[str, str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns ``(kind, description, findings, jobs)``; lists may be empty."""
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None, [], []
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    findings = validate_optional_findings(data.get("findings"))
    jobs = validate_optional_jobs(data.get("jobs"))
    return "fact", description.strip(), findings, jobs
