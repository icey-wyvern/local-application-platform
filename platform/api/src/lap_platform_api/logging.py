from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from datetime import UTC, datetime

REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(value: str) -> Token[str | None]:
    return REQUEST_ID.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    REQUEST_ID.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname.lower(),
            "service": "platform-api",
            "message": record.getMessage(),
        }
        request_id = REQUEST_ID.get()
        if request_id is not None:
            event["request_id"] = request_id
        for key in (
            "method",
            "path",
            "status_code",
            "duration_ms",
            "dependency",
            "error_code",
            "operation_id",
            "operation_type",
            "attempt",
            "lease_owner",
            "claim_kind",
            "outcome",
            "build_id",
            "build_stage",
            "retryable",
            "application_name",
            "release_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                event[key] = value
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event, separators=(",", ":"), ensure_ascii=True)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
