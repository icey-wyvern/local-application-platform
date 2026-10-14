from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lap_platform_api.build_agent import Revision


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(ApiModel):
    status: str


class ApiInfoResponse(ApiModel):
    service: str
    apiVersion: str
    version: str


class DeploymentRequest(ApiModel):
    repositoryUrl: Annotated[str, Field(min_length=1, max_length=2048)]
    revision: Revision


class RollbackRequest(ApiModel):
    releaseId: UUID


class OperationResponse(ApiModel):
    id: UUID
    type: str
    status: str
    applicationId: UUID | None
    buildId: UUID | None
    deploymentId: UUID | None
    attempts: int
    createdAt: datetime
    updatedAt: datetime
    completedAt: datetime | None
    failureCode: str | None
    failureMessage: str | None
    failureResult: dict[str, Any] | None
    pollingLocation: str


class ApplicationResponse(ApiModel):
    id: UUID
    name: str
    repositoryUrl: str
    currentReleaseId: UUID | None
    createdAt: datetime
    updatedAt: datetime


class BuildResponse(ApiModel):
    id: UUID
    operationId: UUID
    applicationId: UUID | None
    repositoryUrl: str
    requestedRevision: str
    resolvedCommit: str | None
    status: str
    releaseId: UUID | None
    createdAt: datetime
    updatedAt: datetime
    startedAt: datetime | None
    completedAt: datetime | None
    failureCode: str | None
    failureMessage: str | None


class ReleaseResponse(ApiModel):
    id: UUID
    applicationId: UUID
    buildId: UUID
    gitCommit: str
    imageReference: str
    imageDigest: str
    port: int
    healthPath: str
    createdAt: datetime


class DeploymentResponse(ApiModel):
    id: UUID
    operationId: UUID
    applicationId: UUID
    desiredReleaseId: UUID
    previousReleaseId: UUID | None
    rollbackOfDeploymentId: UUID | None
    type: str
    status: str
    createdAt: datetime
    updatedAt: datetime
    startedAt: datetime | None
    completedAt: datetime | None
    failureCode: str | None
    failureMessage: str | None
