from __future__ import annotations

import json
import logging

import pytest
from lap_platform_api.logging import JsonFormatter, reset_request_id, set_request_id


@pytest.mark.unit
def test_json_logging_is_structured_and_excludes_unselected_secrets() -> None:
    formatter = JsonFormatter()
    token = set_request_id("00000000-0000-4000-8000-000000000001")
    try:
        record = logging.LogRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            "request completed",
            (),
            None,
        )
        record.method = "GET"
        record.authorization = "Bearer must-not-appear"
        event = json.loads(formatter.format(record))
    finally:
        reset_request_id(token)

    assert event["service"] == "platform-api"
    assert event["severity"] == "info"
    assert event["method"] == "GET"
    assert event["request_id"] == "00000000-0000-4000-8000-000000000001"
    assert "must-not-appear" not in json.dumps(event)


@pytest.mark.unit
def test_scheduler_log_fields_are_structured() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "operation claimed",
        (),
        None,
    )
    record.operation_type = "deploy"
    record.operation_id = "00000000-0000-4000-8000-000000000001"
    record.attempt = 2
    record.claim_kind = "reclaimed"

    event = json.loads(formatter.format(record))

    assert event["operation_type"] == "deploy"
    assert event["attempt"] == 2
    assert event["claim_kind"] == "reclaimed"
