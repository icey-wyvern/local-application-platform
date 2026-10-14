from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    name: str
    port: int
    healthPath: str


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


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    stage: str | None = None
    retryable: bool = False


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
