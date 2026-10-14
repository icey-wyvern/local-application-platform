from __future__ import annotations

import ssl
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from opentelemetry.propagate import inject
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lap_platform_api.config import BuildAgentSettings


class RevisionType(StrEnum):
    BRANCH = "branch"
    TAG = "tag"
    COMMIT = "commit"


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RevisionType
    value: Annotated[str, Field(min_length=1, max_length=255)]

    @field_validator("value")
    @classmethod
    def safe_revision(cls, value: str) -> str:
        if (
            value != value.strip()
            or value.startswith("-")
            or any(char in value for char in ("\x00", "\n", "\r"))
        ):
            raise ValueError("revision contains prohibited characters")
        if ".." in value or "@{" in value or "\\" in value:
            raise ValueError("revision is not a safe Git reference")
        return value


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buildId: UUID
    repositoryUrl: Annotated[str, Field(min_length=1, max_length=2048)]
    revision: Revision


class ApplicationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")]
    port: Annotated[int, Field(ge=1024, le=65535)]
    healthPath: Annotated[str, Field(min_length=1, max_length=256)]

    @field_validator("healthPath")
    @classmethod
    def valid_health_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "?" in value or "#" in value:
            raise ValueError("health path must be an absolute path without query or fragment")
        return value


class VulnerabilitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: str
    counts: dict[str, int]
    blocked: int


class BuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buildId: UUID
    status: Literal["succeeded"] = "succeeded"
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    imageReference: str
    imageDigest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    application: ApplicationMetadata
    vulnerabilities: VulnerabilitySummary

    @model_validator(mode="after")
    def reference_matches_digest(self) -> BuildResult:
        if not self.imageReference.endswith(f"@{self.imageDigest}"):
            raise ValueError("image reference must contain the verified image digest")
        return self


class BuildAgentErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    stage: str | None = None
    retryable: bool = False


class BuildAgentErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: BuildAgentErrorDetail


class BuildAgentError(RuntimeError):
    def __init__(self, status_code: int, detail: BuildAgentErrorDetail) -> None:
        super().__init__(detail.message)
        self.status_code = status_code
        self.detail = detail


def create_mtls_context(settings: BuildAgentSettings) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(settings.ca_certificate))
    context.load_cert_chain(
        certfile=str(settings.client_certificate), keyfile=str(settings.client_key)
    )
    return context


class BuildAgentClient:
    def __init__(
        self,
        settings: BuildAgentSettings,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.url,
            verify=create_mtls_context(settings),
            timeout=httpx.Timeout(
                settings.request_timeout_seconds,
                connect=settings.connect_timeout_seconds,
            ),
            transport=transport,
        )

    async def health(self) -> bool:
        response = await self._client.get("/healthz", headers=self._trace_headers())
        return response.status_code == 200

    async def submit_build(self, request: BuildRequest) -> BuildResult:
        response = await self._client.post(
            "/v1/builds",
            content=request.model_dump_json(),
            headers={"Content-Type": "application/json", **self._trace_headers()},
        )
        if response.is_success:
            return BuildResult.model_validate(response.json())
        raise self._error(response)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _trace_headers() -> dict[str, str]:
        headers: dict[str, str] = {}
        inject(headers)
        return headers

    @staticmethod
    def _error(response: httpx.Response) -> BuildAgentError:
        try:
            parsed = BuildAgentErrorResponse.model_validate(response.json())
        except (ValueError, TypeError):
            parsed = BuildAgentErrorResponse(
                error=BuildAgentErrorDetail(
                    code="build_agent_invalid_response",
                    message="build agent returned an invalid error response",
                    retryable=response.status_code >= 500,
                )
            )
        return BuildAgentError(response.status_code, parsed.error)

    @property
    def configuration(self) -> dict[str, Any]:
        return {
            "url": self._settings.url,
            "connect_timeout_seconds": self._settings.connect_timeout_seconds,
            "request_timeout_seconds": self._settings.request_timeout_seconds,
            "certificate_verification": True,
            "mutual_tls": True,
        }
