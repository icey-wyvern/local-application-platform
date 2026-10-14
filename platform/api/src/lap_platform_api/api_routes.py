from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lap_platform_api.build_agent import RevisionType
from lap_platform_api.errors import PlatformError
from lap_platform_api.idempotency import canonical_request_hash, require_idempotency_key
from lap_platform_api.models import Application, Build, Deployment, Operation, Release
from lap_platform_api.repositories import (
    EntityStateError,
    IdempotencyConflictError,
    create_build,
    create_idempotent_operation,
    get_application_by_name,
    get_build,
    get_deployment,
    get_operation,
    get_operation_by_idempotency_key,
    get_release,
    list_application_builds,
    list_application_deployments,
    list_application_operations,
    list_application_releases,
    list_applications,
    request_manual_rollback,
)
from lap_platform_api.schemas import (
    ApplicationResponse,
    BuildResponse,
    DeploymentRequest,
    DeploymentResponse,
    OperationResponse,
    ReleaseResponse,
    RollbackRequest,
)
from lap_platform_api.states import OperationType

PageOffset = Annotated[int, Query(ge=0)]
PageLimit = Annotated[int, Query(ge=1, le=100)]


def _repository_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise PlatformError(422, "REPOSITORY_URL_INVALID", "repositoryUrl is invalid") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise PlatformError(
            422,
            "REPOSITORY_URL_INVALID",
            "repositoryUrl must be HTTPS without credentials, query, fragment, "
            "or a non-standard port",
        )
    host = parsed.hostname.rstrip(".").casefold()
    if not host:
        raise PlatformError(422, "REPOSITORY_URL_INVALID", "repositoryUrl has no hostname")
    netloc = f"[{host}]" if ":" in host else host
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(("https", netloc, path, "", ""))


def _stored_revision(request: DeploymentRequest) -> str:
    if request.revision.type == RevisionType.BRANCH:
        revision = f"refs/heads/{request.revision.value}"
    elif request.revision.type == RevisionType.TAG:
        revision = f"refs/tags/{request.revision.value}"
    else:
        revision = request.revision.value
    if len(revision) > 255:
        raise PlatformError(
            422,
            "GIT_REVISION_INVALID",
            "revision is too long after conversion to a canonical Git reference",
        )
    return revision


def _operation_response(operation: Operation) -> OperationResponse:
    return OperationResponse(
        id=operation.id,
        type=operation.type,
        status=operation.status,
        applicationId=operation.application_id,
        buildId=operation.build_id,
        deploymentId=operation.deployment_id,
        attempts=operation.attempts,
        createdAt=operation.created_at,
        updatedAt=operation.updated_at,
        completedAt=operation.completed_at,
        failureCode=operation.failure_code,
        failureMessage=operation.failure_message,
        failureResult=operation.failure_result,
        pollingLocation=f"/api/v1/operations/{operation.id}",
    )


def _application_response(application: Application) -> ApplicationResponse:
    return ApplicationResponse(
        id=application.id,
        name=application.name,
        repositoryUrl=application.repository_url,
        currentReleaseId=application.current_release_id,
        createdAt=application.created_at,
        updatedAt=application.updated_at,
    )


def _build_response(build: Build) -> BuildResponse:
    return BuildResponse(
        id=build.id,
        operationId=build.operation_id,
        applicationId=build.application_id,
        repositoryUrl=build.repository_url,
        requestedRevision=build.requested_revision,
        resolvedCommit=build.resolved_commit,
        status=build.status,
        releaseId=build.release.id if build.release is not None else None,
        createdAt=build.created_at,
        updatedAt=build.updated_at,
        startedAt=build.started_at,
        completedAt=build.completed_at,
        failureCode=build.failure_code,
        failureMessage=build.failure_message,
    )


def _release_response(release: Release) -> ReleaseResponse:
    return ReleaseResponse(
        id=release.id,
        applicationId=release.application_id,
        buildId=release.build_id,
        gitCommit=release.git_commit,
        imageReference=release.image_reference,
        imageDigest=release.image_digest,
        port=release.port,
        healthPath=release.health_path,
        createdAt=release.created_at,
    )


def _deployment_response(deployment: Deployment) -> DeploymentResponse:
    return DeploymentResponse(
        id=deployment.id,
        operationId=deployment.operation_id,
        applicationId=deployment.application_id,
        desiredReleaseId=deployment.desired_release_id,
        previousReleaseId=deployment.previous_release_id,
        rollbackOfDeploymentId=deployment.rollback_of_deployment_id,
        type=deployment.type,
        status=deployment.status,
        createdAt=deployment.created_at,
        updatedAt=deployment.updated_at,
        startedAt=deployment.started_at,
        completedAt=deployment.completed_at,
        failureCode=deployment.failure_code,
        failureMessage=deployment.failure_message,
    )


def _active_operation_conflict(error: IntegrityError) -> bool:
    diagnostic = getattr(error.orig, "diag", None)
    constraint = getattr(diagnostic, "constraint_name", None)
    return constraint in {
        "uq_operations_active_application",
        "uq_operations_active_deploy_repository",
        "uq_deployments_active_application",
    }


def _application_or_404(session: Session, name: str) -> Application:
    application = get_application_by_name(session, name)
    if application is None:
        raise PlatformError(404, "APPLICATION_NOT_FOUND", "application was not found")
    return application


def create_public_router(*, authentication_dependency: Any, database_dependency: Any) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        dependencies=[Depends(authentication_dependency)],
    )

    @router.post(
        "/deployments",
        response_model=OperationResponse,
        status_code=202,
        responses={202: {"description": "Deployment intent accepted"}},
    )
    def submit_deployment(
        body: DeploymentRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[UUID, Depends(require_idempotency_key)],
        session: Annotated[Session, Depends(database_dependency)],
    ) -> OperationResponse:
        repository_url = _repository_url(body.repositoryUrl)
        revision = _stored_revision(body)
        request_hash = canonical_request_hash(
            operation=OperationType.DEPLOY.value,
            content={
                "repositoryUrl": repository_url,
                "revision": body.revision.model_dump(mode="json"),
            },
        )
        try:
            with session.begin():
                operation, created = create_idempotent_operation(
                    session,
                    operation_type=OperationType.DEPLOY,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    correlation_id=UUID(request.state.correlation_id),
                    repository_url=body.repositoryUrl,
                    normalized_repository_url=repository_url,
                    git_revision=revision,
                )
                if created:
                    create_build(
                        session,
                        operation=operation,
                        repository_url=body.repositoryUrl,
                        normalized_repository_url=repository_url,
                        requested_revision=revision,
                    )
        except IdempotencyConflictError as error:
            raise PlatformError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used for a different request",
            ) from error
        except IntegrityError as error:
            if _active_operation_conflict(error):
                raise PlatformError(
                    409,
                    "APPLICATION_BUSY",
                    "an active deployment operation already exists for this repository",
                ) from error
            raise
        response.headers["Location"] = f"/api/v1/operations/{operation.id}"
        return _operation_response(operation)

    @router.get("/operations/{operation_id}", response_model=OperationResponse)
    def read_operation(
        operation_id: UUID,
        session: Annotated[Session, Depends(database_dependency)],
    ) -> OperationResponse:
        operation = get_operation(session, operation_id)
        if operation is None:
            raise PlatformError(404, "OPERATION_NOT_FOUND", "operation was not found")
        return _operation_response(operation)

    @router.get("/applications", response_model=list[ApplicationResponse])
    def read_applications(
        session: Annotated[Session, Depends(database_dependency)],
        offset: PageOffset = 0,
        limit: PageLimit = 100,
    ) -> list[ApplicationResponse]:
        return [
            _application_response(application)
            for application in list_applications(session, offset=offset, limit=limit)
        ]

    @router.get("/applications/{name}", response_model=ApplicationResponse)
    def read_application(
        name: str,
        session: Annotated[Session, Depends(database_dependency)],
    ) -> ApplicationResponse:
        return _application_response(_application_or_404(session, name))

    @router.post(
        "/applications/{name}/rollbacks",
        response_model=OperationResponse,
        status_code=202,
        responses={202: {"description": "Historical Release activation accepted"}},
    )
    def submit_rollback(
        name: str,
        body: RollbackRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[UUID, Depends(require_idempotency_key)],
        session: Annotated[Session, Depends(database_dependency)],
    ) -> OperationResponse:
        request_hash = canonical_request_hash(
            operation=OperationType.ROLLBACK.value,
            content={"applicationName": name, "releaseId": str(body.releaseId)},
        )
        try:
            with session.begin():
                operation = request_manual_rollback(
                    session,
                    application_name=name,
                    release_id=body.releaseId,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    correlation_id=UUID(request.state.correlation_id),
                )
        except IdempotencyConflictError as error:
            raise PlatformError(
                409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for another request"
            ) from error
        except EntityStateError as error:
            raise PlatformError(409, "ROLLBACK_TARGET_INVALID", str(error)) from error
        except IntegrityError as error:
            if _active_operation_conflict(error):
                raise PlatformError(
                    409,
                    "APPLICATION_BUSY",
                    "an active operation already exists for this application",
                ) from error
            raise
        response.headers["Location"] = f"/api/v1/operations/{operation.id}"
        return _operation_response(operation)

    @router.delete(
        "/applications/{name}",
        response_model=OperationResponse,
        status_code=202,
        responses={202: {"description": "Deletion intent accepted"}},
    )
    def delete_application(
        name: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[UUID, Depends(require_idempotency_key)],
        session: Annotated[Session, Depends(database_dependency)],
    ) -> OperationResponse:
        request_hash = canonical_request_hash(
            operation=OperationType.DELETE.value,
            content={"applicationName": name},
        )
        try:
            with session.begin():
                operation = get_operation_by_idempotency_key(session, idempotency_key)
                if operation is not None:
                    if operation.request_hash != request_hash:
                        raise IdempotencyConflictError(
                            "idempotency key was used for a different request"
                        )
                else:
                    application = _application_or_404(session, name)
                    operation, _created = create_idempotent_operation(
                        session,
                        operation_type=OperationType.DELETE,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        correlation_id=UUID(request.state.correlation_id),
                        application_id=application.id,
                    )
        except IdempotencyConflictError as error:
            raise PlatformError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used for a different request",
            ) from error
        except IntegrityError as error:
            if _active_operation_conflict(error):
                raise PlatformError(
                    409,
                    "APPLICATION_BUSY",
                    "an active operation already exists for this application",
                ) from error
            raise
        response.headers["Location"] = f"/api/v1/operations/{operation.id}"
        return _operation_response(operation)

    @router.get("/applications/{name}/builds", response_model=list[BuildResponse])
    def read_application_builds(
        name: str,
        session: Annotated[Session, Depends(database_dependency)],
        offset: PageOffset = 0,
        limit: PageLimit = 100,
    ) -> list[BuildResponse]:
        application = _application_or_404(session, name)
        return [
            _build_response(build)
            for build in list_application_builds(
                session, application.id, offset=offset, limit=limit
            )
        ]

    @router.get("/applications/{name}/releases", response_model=list[ReleaseResponse])
    def read_application_releases(
        name: str,
        session: Annotated[Session, Depends(database_dependency)],
        offset: PageOffset = 0,
        limit: PageLimit = 100,
    ) -> list[ReleaseResponse]:
        application = _application_or_404(session, name)
        return [
            _release_response(release)
            for release in list_application_releases(
                session, application.id, offset=offset, limit=limit
            )
        ]

    @router.get("/applications/{name}/deployments", response_model=list[DeploymentResponse])
    def read_application_deployments(
        name: str,
        session: Annotated[Session, Depends(database_dependency)],
        offset: PageOffset = 0,
        limit: PageLimit = 100,
    ) -> list[DeploymentResponse]:
        application = _application_or_404(session, name)
        return [
            _deployment_response(deployment)
            for deployment in list_application_deployments(
                session, application.id, offset=offset, limit=limit
            )
        ]

    @router.get("/applications/{name}/operations", response_model=list[OperationResponse])
    def read_application_operations(
        name: str,
        session: Annotated[Session, Depends(database_dependency)],
        offset: PageOffset = 0,
        limit: PageLimit = 100,
    ) -> list[OperationResponse]:
        application = _application_or_404(session, name)
        return [
            _operation_response(operation)
            for operation in list_application_operations(
                session, application.id, offset=offset, limit=limit
            )
        ]

    @router.get("/builds/{build_id}", response_model=BuildResponse)
    def read_build(
        build_id: UUID,
        session: Annotated[Session, Depends(database_dependency)],
    ) -> BuildResponse:
        build = get_build(session, build_id)
        if build is None:
            raise PlatformError(404, "BUILD_NOT_FOUND", "build was not found")
        return _build_response(build)

    @router.get("/releases/{release_id}", response_model=ReleaseResponse)
    def read_release(
        release_id: UUID,
        session: Annotated[Session, Depends(database_dependency)],
    ) -> ReleaseResponse:
        release = get_release(session, release_id)
        if release is None:
            raise PlatformError(404, "RELEASE_NOT_FOUND", "release was not found")
        return _release_response(release)

    @router.get("/deployments/{deployment_id}", response_model=DeploymentResponse)
    def read_deployment(
        deployment_id: UUID,
        session: Annotated[Session, Depends(database_dependency)],
    ) -> DeploymentResponse:
        deployment = get_deployment(session, deployment_id)
        if deployment is None:
            raise PlatformError(404, "DEPLOYMENT_NOT_FOUND", "deployment was not found")
        return _deployment_response(deployment)

    return router
