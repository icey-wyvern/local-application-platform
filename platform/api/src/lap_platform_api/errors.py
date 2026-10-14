from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")


class ProblemDetail(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: str
    title: str
    status: int
    detail: str
    instance: str
    error_code: str = Field(alias="errorCode")
    correlation_id: str = Field(alias="correlationId")
    extensions: dict[str, Any] | None = None


class PlatformError(Exception):
    def __init__(
        self,
        status_code: int,
        error_code: str,
        detail: str,
        *,
        title: str | None = None,
        headers: dict[str, str] | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        if ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError("error code must be uppercase snake case")
        super().__init__(detail)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail
        self.title = title or HTTPStatus(status_code).phrase
        self.headers = headers or {}
        self.extensions = extensions


def problem_type(error_code: str) -> str:
    return f"https://local-application-platform.dev/problems/{error_code}"
