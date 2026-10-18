from __future__ import annotations

import json
from typing import get_args

import pytest

from board.dispatcher.contracts import (
    parse_json_output,
    validate_explore_payload,
    validate_optional_findings,
    validate_reason_payload,
)
from board.dispatcher.runtime.process import ManagedProcess
from board.dispatcher.workers.adapters.pi import PiDriver
from board.server.models import FindingStatus


def test_parse_json_output_extracts_object_from_markdown_noise() -> None:
    assert parse_json_output('result:\n```json\n{"accepted": true, "data": {}}\n```') == {
        "accepted": True,
        "data": {},
    }


def test_reason_payload_limits_number_of_intents() -> None:
    kind, intents = validate_reason_payload(
        {
            "accepted": True,
            "data": {
                "intents": [
                    {"from": ["f001"], "description": "one"},
                    {"from": ["f001"], "description": "two"},
                ]
            },
        },
        open_intents_empty=True,
        max_intents=1,
    )

    assert kind == "intents"
    assert intents == [{"from": ["f001"], "description": "one"}]


def test_reason_payload_requires_intent_when_none_are_open() -> None:
    with pytest.raises(ValueError, match="intents or an explicit wait reason is required"):
        validate_reason_payload(
            {"accepted": True, "data": {}},
            open_intents_empty=True,
            max_intents=3,
        )


def test_explore_payload_rejects_planning_text() -> None:
    with pytest.raises(ValueError):
        validate_explore_payload(parse_json_output("Need inspect files and keep working."))


@pytest.mark.parametrize("status", get_args(FindingStatus))
def test_worker_findings_preserve_api_status(status: str) -> None:
    kind, description, findings, jobs = validate_explore_payload(
        {
            "accepted": True,
            "data": {
                "description": "Candidate reviewed against source.",
                "findings": [{"title": "Reviewed candidate", "severity": "high", "status": status}],
            },
        }
    )

    assert kind == "fact"
    assert findings[0]["status"] == status


@pytest.mark.parametrize("status", [None, "resolved", "", [], {}, True, 1])
def test_worker_findings_reject_invalid_status(status) -> None:
    with pytest.raises(ValueError, match=r"findings\[0\]\.status must be one of"):
        validate_optional_findings([{"title": "Candidate", "status": status}])


def test_worker_findings_without_status_keep_api_default() -> None:
    assert validate_optional_findings([{"title": "Candidate"}]) == [
        {"title": "Candidate", "severity": "info", "description": "Candidate"}
    ]


def test_pi_driver_extracts_session_and_last_assistant_text() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-123"}),
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}],
                    },
                }
            ),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "session-123"
    assert driver.extract_response_text(stdout, "") == '{"accepted":true,"data":{}}'


def test_close_stream_closes_response_even_when_stream_close_fails() -> None:
    class Response:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Stream:
        def __init__(self) -> None:
            self._response = Response()

        def close(self) -> None:
            raise ValueError("already closed")

    stream = Stream()
    ManagedProcess._close_stream(stream)

    assert stream._response.closed

