from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import Header

from lap_platform_api.errors import PlatformError


def canonical_request_hash(*, operation: str, content: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"operation": operation, "content": content},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def require_idempotency_key(
    value: str | None = Header(default=None, alias="Idempotency-Key"),
) -> UUID:
    try:
        key = UUID(value) if value is not None else None
    except (ValueError, AttributeError):
        key = None
    if key is None or key.version != 4 or str(key) != value:
        raise PlatformError(
            400,
            "IDEMPOTENCY_KEY_INVALID",
            "Idempotency-Key must be a canonical UUID version 4",
        )
    return key
