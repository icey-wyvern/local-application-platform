from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lap_platform_api.models import Application, Build, Deployment, Operation, Release
from lap_platform_api.states import (
    BuildStatus,
    DeploymentStatus,
    DeploymentType,
    OperationStatus,
    OperationType,
)

OPERATION_LEASE_DURATION = timedelta(seconds=45)
OPERATION_LEASE_RENEWAL_INTERVAL = timedelta(seconds=10)


@dataclass(frozen=True)
class OperationClaim:
    id: UUID
    type: str
    correlation_id: UUID
    attempt: int
    reclaimed: bool


class IdempotencyConflictError(ValueError):
    """The supplied key was already used for different canonical request content."""


class EntityStateError(ValueError):
    """The requested relationship is inconsistent with durable entity state."""


class OperationLeaseLostError(RuntimeError):
    """The operation is no longer owned by the worker attempting a durable write."""


@dataclass(frozen=True)
class BuildDispatchRecord:
    id: UUID
    repository_url: str
    normalized_repository_url: str
    requested_revision: str
    status: str
    release_id: UUID | None
    failure_code: str | None
    failure_message: str | None


@dataclass(frozen=True)
class ReleasePersistenceResult:
    release: Release
    created: bool


@dataclass(frozen=True)
class ReleaseDeploymentIntent:
    deployment_id: UUID
    application_name: str
    release_id: UUID
    image_reference: str
    port: int
    health_path: str
    started_at: datetime
    status: str


@dataclass(frozen=True)
class RollbackState:
    failed_deployment_id: UUID
    failed_release_id: UUID
    failure_code: str
    failure_message: str
    rollback: ReleaseDeploymentIntent | None
    unavailable: bool = False
    rollback_failure_code: str | None = None
    rollback_failure_message: str | None = None


def create_application(
    session: Session,
    *,
    name: str,
    repository_url: str,
    normalized_repository_url: str,
) -> Application:
    application = Application(
        name=name,
        repository_url=repository_url,
        normalized_repository_url=normalized_repository_url,
    )
    session.add(application)
    session.flush()
    return application


def get_application(
    session: Session, application_id: UUID, *, include_deleted: bool = False
) -> Application | None:
    statement = select(Application).where(Application.id == application_id)
    if not include_deleted:
        statement = statement.where(Application.deleted_at.is_(None))
    return session.scalar(statement)


def get_application_by_name(
    session: Session, name: str, *, include_deleted: bool = False
) -> Application | None:
    statement = select(Application).where(Application.name == name)
    if not include_deleted:
        statement = statement.where(Application.deleted_at.is_(None))
    return session.scalar(statement)


def list_applications(session: Session, *, offset: int = 0, limit: int = 100) -> list[Application]:
    return list(
        session.scalars(
            select(Application)
            .where(Application.deleted_at.is_(None))
            .order_by(Application.created_at, Application.id)
            .offset(offset)
            .limit(limit)
        )
    )


def get_operation(session: Session, operation_id: UUID) -> Operation | None:
    return session.get(Operation, operation_id)


def get_build(session: Session, build_id: UUID) -> Build | None:
    return session.get(Build, build_id)


def get_release(session: Session, release_id: UUID) -> Release | None:
    return session.get(Release, release_id)


def get_deployment(session: Session, deployment_id: UUID) -> Deployment | None:
    return session.get(Deployment, deployment_id)


def list_application_builds(
    session: Session, application_id: UUID, *, offset: int = 0, limit: int = 100
) -> list[Build]:
    return list(
        session.scalars(
            select(Build)
            .where(Build.application_id == application_id)
            .order_by(Build.created_at.desc(), Build.id)
            .offset(offset)
            .limit(limit)
        )
    )


def list_application_releases(
    session: Session, application_id: UUID, *, offset: int = 0, limit: int = 100
) -> list[Release]:
    return list(
        session.scalars(
            select(Release)
            .where(Release.application_id == application_id)
            .order_by(Release.created_at.desc(), Release.id)
            .offset(offset)
            .limit(limit)
        )
    )


def list_application_deployments(
    session: Session, application_id: UUID, *, offset: int = 0, limit: int = 100
) -> list[Deployment]:
    return list(
        session.scalars(
            select(Deployment)
            .where(Deployment.application_id == application_id)
            .order_by(Deployment.created_at.desc(), Deployment.id)
            .offset(offset)
            .limit(limit)
        )
    )


def list_application_operations(
    session: Session, application_id: UUID, *, offset: int = 0, limit: int = 100
) -> list[Operation]:
    return list(
        session.scalars(
            select(Operation)
            .where(Operation.application_id == application_id)
            .order_by(Operation.created_at.desc(), Operation.id)
            .offset(offset)
            .limit(limit)
        )
    )


def soft_delete_application(
    session: Session, application: Application, *, deleted_at: datetime | None = None
) -> Application:
    if application.deleted_at is None:
        application.deleted_at = deleted_at or datetime.now(UTC)
        session.flush()
    return application


def soft_delete_for_operation(session: Session, *, operation_id: UUID, lease_owner: str) -> None:
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    if operation.type != OperationType.DELETE.value or operation.application_id is None:
        raise EntityStateError("delete operation has no application")
    application = get_application(session, operation.application_id, include_deleted=True)
    if application is None:
        raise EntityStateError("delete operation references a missing application")
    soft_delete_application(session, application)


def create_idempotent_operation(
    session: Session,
    *,
    operation_type: OperationType,
    idempotency_key: UUID,
    request_hash: str,
    correlation_id: UUID,
    repository_url: str | None = None,
    normalized_repository_url: str | None = None,
    git_revision: str | None = None,
    application_id: UUID | None = None,
) -> tuple[Operation, bool]:
    # A duplicate insert can otherwise deadlock when PostgreSQL speculatively
    # checks both the idempotency key and an active-work partial unique index.
    # This transaction lock serializes only requests whose keys map to the same
    # 64-bit value; the unique constraint remains the authoritative boundary.
    lock_id = int.from_bytes(idempotency_key.bytes[:8], signed=True)
    session.execute(select(func.pg_advisory_xact_lock(lock_id)))
    values = {
        "type": operation_type.value,
        "status": OperationStatus.QUEUED.value,
        "idempotency_key": idempotency_key,
        "request_hash": request_hash,
        "correlation_id": correlation_id,
        "repository_url": repository_url,
        "normalized_repository_url": normalized_repository_url,
        "git_revision": git_revision,
        "application_id": application_id,
    }
    inserted_id = session.scalar(
        insert(Operation)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Operation.idempotency_key])
        .returning(Operation.id)
    )
    if inserted_id is not None:
        operation = session.get(Operation, inserted_id)
        if operation is None:  # pragma: no cover - guarded by the returning insert
            raise RuntimeError("inserted operation could not be loaded")
        return operation, True

    operation = session.scalar(
        select(Operation).where(Operation.idempotency_key == idempotency_key)
    )
    if operation is None:  # pragma: no cover - would require external deletion during transaction
        raise RuntimeError("idempotent operation disappeared")
    if operation.request_hash != request_hash:
        raise IdempotencyConflictError("idempotency key was used for a different request")
    return operation, False


def get_operation_by_idempotency_key(session: Session, idempotency_key: UUID) -> Operation | None:
    return session.scalar(select(Operation).where(Operation.idempotency_key == idempotency_key))


def create_build(
    session: Session,
    *,
    operation: Operation,
    repository_url: str,
    normalized_repository_url: str,
    requested_revision: str,
    application: Application | None = None,
) -> Build:
    if operation.type != OperationType.DEPLOY.value:
        raise EntityStateError("only deploy operations may own builds")
    if operation.build_id is not None or operation.builds:
        raise EntityStateError("operation already has a build")
    build = Build(
        operation=operation,
        application=application,
        repository_url=repository_url,
        normalized_repository_url=normalized_repository_url,
        requested_revision=requested_revision,
        status=BuildStatus.PENDING.value,
    )
    session.add(build)
    session.flush()
    operation.build = build
    session.flush()
    return build


def create_release(
    session: Session,
    *,
    build: Build,
    application: Application,
    image_reference: str,
    image_digest: str,
    port: int,
    health_path: str,
) -> Release:
    if build.status != BuildStatus.SUCCEEDED.value or build.resolved_commit is None:
        raise EntityStateError("a release requires a successful build with a resolved commit")
    if build.application_id != application.id:
        raise EntityStateError("build and release must belong to the same application")
    release = Release(
        application=application,
        build=build,
        git_commit=build.resolved_commit,
        image_reference=image_reference,
        image_digest=image_digest,
        port=port,
        health_path=health_path,
    )
    session.add(release)
    session.flush()
    return release


def create_deployment(
    session: Session,
    *,
    operation: Operation,
    application: Application,
    desired_release: Release,
    deployment_type: DeploymentType,
    previous_release: Release | None = None,
    rollback_of: Deployment | None = None,
) -> Deployment:
    if desired_release.application_id != application.id:
        raise EntityStateError("desired release belongs to another application")
    if previous_release is not None and previous_release.application_id != application.id:
        raise EntityStateError("previous release belongs to another application")
    if rollback_of is not None and rollback_of.application_id != application.id:
        raise EntityStateError("rollback target belongs to another application")
    deployment = Deployment(
        operation=operation,
        application=application,
        desired_release=desired_release,
        previous_release=previous_release,
        rollback_of=rollback_of,
        type=deployment_type.value,
        status=DeploymentStatus.PENDING.value,
    )
    session.add(deployment)
    session.flush()
    operation.deployment = deployment
    session.flush()
    return deployment


def request_manual_rollback(
    session: Session,
    *,
    application_name: str,
    release_id: UUID,
    idempotency_key: UUID,
    request_hash: str,
    correlation_id: UUID,
) -> Operation:
    """Record a rollback to a Release proven healthy for this Application."""
    existing = get_operation_by_idempotency_key(session, idempotency_key)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyConflictError("idempotency key was used for a different request")
        return existing
    application = session.scalar(
        select(Application).where(Application.name == application_name).with_for_update()
    )
    if application is None or application.deleted_at is not None:
        raise EntityStateError("application was not found")
    target = session.get(Release, release_id)
    if target is None or target.application_id != application.id:
        raise EntityStateError("target Release does not belong to this Application")
    if application.current_release_id is None or application.current_release_id == target.id:
        raise EntityStateError("target Release is already current or no healthy Release is current")
    if target.build.status != BuildStatus.SUCCEEDED.value:
        raise EntityStateError("target Release does not have a successful Build")
    proven = session.scalar(
        select(Deployment.id)
        .where(
            Deployment.application_id == application.id,
            Deployment.desired_release_id == target.id,
            Deployment.status == DeploymentStatus.HEALTHY.value,
        )
        .limit(1)
    )
    if proven is None:
        raise EntityStateError("target Release has no healthy Deployment history")
    operation, created = create_idempotent_operation(
        session,
        operation_type=OperationType.ROLLBACK,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        correlation_id=correlation_id,
        application_id=application.id,
    )
    if created:
        create_deployment(
            session,
            operation=operation,
            application=application,
            desired_release=target,
            previous_release=application.current_release,
            deployment_type=DeploymentType.MANUAL_ROLLBACK,
        )
    return operation


def _leased_operation(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
) -> Operation:
    operation = session.scalar(
        select(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.RUNNING.value,
            Operation.lease_owner == lease_owner,
            Operation.lease_expires_at > func.now(),
        )
        .with_for_update()
    )
    if operation is None:
        raise OperationLeaseLostError("operation lease is no longer valid")
    return operation


def _operation_build(session: Session, operation: Operation) -> Build:
    if operation.type != OperationType.DEPLOY.value or operation.build_id is None:
        raise EntityStateError("deploy operation does not have an associated build")
    build = session.scalar(select(Build).where(Build.id == operation.build_id).with_for_update())
    if build is None or build.operation_id != operation.id:
        raise EntityStateError("deploy operation has an inconsistent build association")
    return build


def prepare_build_dispatch(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
) -> BuildDispatchRecord:
    """Load a build under its authoritative lease and mark a dispatch as started."""
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    build = _operation_build(session, operation)
    release_id = session.scalar(select(Release.id).where(Release.build_id == build.id))
    if build.status not in {
        BuildStatus.SUCCEEDED.value,
        BuildStatus.FAILED.value,
        BuildStatus.REJECTED.value,
    }:
        build.status = BuildStatus.CLONING.value
        build.started_at = build.started_at or datetime.now(UTC)
        build.completed_at = None
        build.failure_code = None
        build.failure_message = None
        session.flush()
    return BuildDispatchRecord(
        id=build.id,
        repository_url=build.repository_url,
        normalized_repository_url=build.normalized_repository_url,
        requested_revision=build.requested_revision,
        status=build.status,
        release_id=release_id,
        failure_code=build.failure_code,
        failure_message=build.failure_message,
    )


def record_retryable_build_failure(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    failure_code: str,
    failure_message: str,
) -> None:
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    build = _operation_build(session, operation)
    if build.status in {
        BuildStatus.SUCCEEDED.value,
        BuildStatus.FAILED.value,
        BuildStatus.REJECTED.value,
    }:
        return
    build.status = BuildStatus.PENDING.value
    build.completed_at = None
    build.failure_code = failure_code
    build.failure_message = failure_message
    session.flush()


def record_terminal_build_failure(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    failure_code: str,
    failure_message: str,
    rejected: bool = False,
) -> None:
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    build = _operation_build(session, operation)
    if build.status == BuildStatus.SUCCEEDED.value:
        raise EntityStateError("a successful build cannot be changed to failed")
    if build.status in {BuildStatus.FAILED.value, BuildStatus.REJECTED.value}:
        return
    build.status = BuildStatus.REJECTED.value if rejected else BuildStatus.FAILED.value
    build.completed_at = datetime.now(UTC)
    build.failure_code = failure_code
    build.failure_message = failure_message
    session.flush()


def record_successful_build(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    application_name: str,
    resolved_commit: str,
    image_reference: str,
    image_digest: str,
    port: int,
    health_path: str,
    vulnerability_counts: dict[str, int],
) -> ReleasePersistenceResult:
    """Atomically persist validated build metadata and its immutable Release."""
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    build = _operation_build(session, operation)
    existing_release = session.scalar(
        select(Release).where(Release.build_id == build.id).with_for_update()
    )
    if existing_release is not None:
        expected = (
            application_name,
            resolved_commit,
            image_reference,
            image_digest,
            port,
            health_path,
        )
        actual = (
            existing_release.application.name,
            existing_release.git_commit,
            existing_release.image_reference,
            existing_release.image_digest,
            existing_release.port,
            existing_release.health_path,
        )
        if actual != expected:
            raise EntityStateError("existing release does not match the build-agent result")
        return ReleasePersistenceResult(existing_release, False)
    if build.status in {BuildStatus.FAILED.value, BuildStatus.REJECTED.value}:
        raise EntityStateError("a terminal failed build cannot create a release")

    application = session.scalar(
        select(Application).where(Application.name == application_name).with_for_update()
    )
    repository_application = session.scalar(
        select(Application)
        .where(Application.normalized_repository_url == build.normalized_repository_url)
        .with_for_update()
    )
    if application is None:
        if repository_application is not None:
            raise EntityStateError("repository is permanently assigned to another application")
        application = create_application(
            session,
            name=application_name,
            repository_url=build.repository_url,
            normalized_repository_url=build.normalized_repository_url,
        )
    elif application.normalized_repository_url != build.normalized_repository_url:
        raise EntityStateError("application name is permanently assigned to another repository")
    elif application.deleted_at is not None:
        raise EntityStateError("a deleted application identity cannot be reused")
    elif repository_application is not None and repository_application.id != application.id:
        raise EntityStateError("repository is permanently assigned to another application")

    if operation.application_id is not None and operation.application_id != application.id:
        raise EntityStateError("operation belongs to another application")
    build.application = application
    build.resolved_commit = resolved_commit
    build.vulnerability_counts = vulnerability_counts
    build.status = BuildStatus.SUCCEEDED.value
    build.completed_at = datetime.now(UTC)
    build.failure_code = None
    build.failure_message = None
    # The operation-link trigger validates against the already-persisted Build row,
    # so establish the Build association before assigning the Operation association.
    session.flush([build])
    operation.application = application
    session.flush([operation])
    release = create_release(
        session,
        build=build,
        application=application,
        image_reference=image_reference,
        image_digest=image_digest,
        port=port,
        health_path=health_path,
    )
    return ReleasePersistenceResult(release, True)


def prepare_release_deployment(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
) -> ReleaseDeploymentIntent:
    """Create or resume the durable Deployment intent for a successful Build."""
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    build = _operation_build(session, operation)
    release = session.scalar(select(Release).where(Release.build_id == build.id).with_for_update())
    if build.status != BuildStatus.SUCCEEDED.value or release is None:
        raise EntityStateError("deploy operation does not have a successful Release")
    application = session.scalar(
        select(Application).where(Application.id == release.application_id).with_for_update()
    )
    if application is None or application.deleted_at is not None:
        raise EntityStateError("deploy operation Application is unavailable")

    deployment: Deployment | None = None
    if operation.deployment_id is not None:
        deployment = session.scalar(
            select(Deployment).where(Deployment.id == operation.deployment_id).with_for_update()
        )
        if (
            deployment is None
            or deployment.operation_id != operation.id
            or deployment.application_id != application.id
            or deployment.desired_release_id != release.id
        ):
            raise EntityStateError("deploy operation has an inconsistent Deployment intent")
    else:
        previous_release = application.current_release
        if previous_release is not None and previous_release.id == release.id:
            previous_release = None
        deployment = create_deployment(
            session,
            operation=operation,
            application=application,
            desired_release=release,
            deployment_type=DeploymentType.RELEASE,
            previous_release=previous_release,
        )

    if deployment.status == DeploymentStatus.PENDING.value:
        deployment.status = DeploymentStatus.APPLYING.value
        deployment.started_at = deployment.started_at or datetime.now(UTC)
        deployment.failure_code = None
        deployment.failure_message = None
        session.flush()
    elif deployment.status not in {
        DeploymentStatus.APPLYING.value,
        DeploymentStatus.RECONCILING.value,
        DeploymentStatus.VERIFYING.value,
        DeploymentStatus.HEALTHY.value,
    }:
        raise EntityStateError("Deployment intent is already terminal")

    return ReleaseDeploymentIntent(
        deployment_id=deployment.id,
        application_name=application.name,
        release_id=release.id,
        image_reference=release.image_reference,
        port=release.port,
        health_path=release.health_path,
        started_at=deployment.started_at or deployment.created_at,
        status=deployment.status,
    )


def prepare_manual_rollback(
    session: Session, *, operation_id: UUID, lease_owner: str
) -> ReleaseDeploymentIntent:
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    if operation.type != OperationType.ROLLBACK.value or operation.deployment_id is None:
        raise EntityStateError("operation has no manual rollback intent")
    deployment = session.scalar(
        select(Deployment).where(Deployment.id == operation.deployment_id).with_for_update()
    )
    if (
        deployment is None
        or deployment.operation_id != operation.id
        or deployment.type != DeploymentType.MANUAL_ROLLBACK.value
    ):
        raise EntityStateError("manual rollback Deployment intent is inconsistent")
    application = session.scalar(
        select(Application).where(Application.id == deployment.application_id).with_for_update()
    )
    target = session.get(Release, deployment.desired_release_id)
    if application is None or target is None or application.deleted_at is not None:
        raise EntityStateError("manual rollback Application or Release is unavailable")
    if target.application_id != application.id:
        raise EntityStateError("manual rollback target belongs to another Application")
    if application.current_release_id not in {
        deployment.previous_release_id,
        deployment.desired_release_id,
    }:
        raise EntityStateError("Application current Release changed during rollback")
    if deployment.status == DeploymentStatus.PENDING.value:
        deployment.status = DeploymentStatus.APPLYING.value
        deployment.started_at = deployment.started_at or datetime.now(UTC)
        session.flush()
    elif deployment.status not in {
        DeploymentStatus.APPLYING.value,
        DeploymentStatus.RECONCILING.value,
        DeploymentStatus.VERIFYING.value,
        DeploymentStatus.HEALTHY.value,
    }:
        raise EntityStateError("manual rollback Deployment is already terminal")
    return ReleaseDeploymentIntent(
        deployment_id=deployment.id,
        application_name=application.name,
        release_id=target.id,
        image_reference=target.image_reference,
        port=target.port,
        health_path=target.health_path,
        started_at=deployment.started_at or deployment.created_at,
        status=deployment.status,
    )


def assert_manual_rollback_apply_allowed(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    current_release_id: UUID | None,
) -> None:
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    deployment = session.get(Deployment, operation.deployment_id)
    if deployment is None or deployment.type != DeploymentType.MANUAL_ROLLBACK.value:
        raise EntityStateError("operation has no manual rollback intent")
    if current_release_id not in {
        deployment.previous_release_id,
        deployment.desired_release_id,
    }:
        raise EntityStateError("Application desired Release changed during rollback")


def assert_release_apply_allowed(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    current_release_id: UUID | None,
) -> None:
    """Verify lease ownership and prevent an older Release replacing a newer CR."""
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    if operation.deployment_id is None:
        raise EntityStateError("deploy operation has no Deployment intent")
    deployment = session.scalar(
        select(Deployment).where(Deployment.id == operation.deployment_id).with_for_update()
    )
    if deployment is None:
        raise EntityStateError("deploy operation Deployment intent is missing")
    if current_release_id is None or current_release_id == deployment.desired_release_id:
        return

    desired = session.get(Release, deployment.desired_release_id)
    current = session.get(Release, current_release_id)
    if desired is None or current is None or current.application_id != deployment.application_id:
        raise EntityStateError("Application CR references an unknown Release")
    desired_order = (desired.created_at, desired.id.int)
    current_order = (current.created_at, current.id.int)
    if current_order > desired_order:
        raise EntityStateError("desired Release has been superseded by a newer Release")


def record_deployment_observed(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
) -> None:
    """Persist that the controller accepted the desired Application generation."""
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    if operation.deployment_id is None:
        raise EntityStateError("deploy operation has no Deployment intent")
    deployment = session.scalar(
        select(Deployment).where(Deployment.id == operation.deployment_id).with_for_update()
    )
    if deployment is None or deployment.operation_id != operation.id:
        raise EntityStateError("deploy operation Deployment intent is missing")
    if deployment.status == DeploymentStatus.APPLYING.value:
        deployment.status = DeploymentStatus.RECONCILING.value
        session.flush()
    elif deployment.status not in {
        DeploymentStatus.RECONCILING.value,
        DeploymentStatus.VERIFYING.value,
        DeploymentStatus.HEALTHY.value,
    }:
        raise EntityStateError("Deployment intent cannot record controller observation")


def record_deployment_verifying(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
) -> None:
    """Persist that all controller-owned readiness gates have passed."""
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    if operation.deployment_id is None:
        raise EntityStateError("deploy operation has no Deployment intent")
    deployment = session.scalar(
        select(Deployment).where(Deployment.id == operation.deployment_id).with_for_update()
    )
    if deployment is None or deployment.operation_id != operation.id:
        raise EntityStateError("deploy operation Deployment intent is missing")
    if deployment.status == DeploymentStatus.RECONCILING.value:
        deployment.status = DeploymentStatus.VERIFYING.value
        session.flush()
    elif deployment.status not in {
        DeploymentStatus.VERIFYING.value,
        DeploymentStatus.HEALTHY.value,
    }:
        raise EntityStateError("Deployment intent cannot verify external health")


def record_healthy_deployment(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
) -> None:
    """Atomically make the accepted Release current and Deployment healthy."""
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    if operation.deployment_id is None:
        raise EntityStateError("deploy operation has no Deployment intent")
    deployment = session.scalar(
        select(Deployment).where(Deployment.id == operation.deployment_id).with_for_update()
    )
    application = session.scalar(
        select(Application).where(Application.id == operation.application_id).with_for_update()
    )
    if deployment is None or deployment.operation_id != operation.id or application is None:
        raise EntityStateError("deploy operation Deployment intent is missing")
    if deployment.status == DeploymentStatus.HEALTHY.value:
        if application.current_release_id != deployment.desired_release_id:
            raise EntityStateError("healthy Deployment does not match current Release")
        return
    if deployment.status != DeploymentStatus.VERIFYING.value:
        raise EntityStateError("Deployment intent is not ready for healthy completion")
    deployment.status = DeploymentStatus.HEALTHY.value
    deployment.completed_at = datetime.now(UTC)
    deployment.failure_code = None
    deployment.failure_message = None
    application.current_release_id = deployment.desired_release_id
    session.flush()


def record_terminal_deployment_failure(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    failure_code: str,
    failure_message: str,
) -> None:
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    if operation.deployment_id is None:
        return
    deployment = session.scalar(
        select(Deployment).where(Deployment.id == operation.deployment_id).with_for_update()
    )
    if deployment is None:
        raise EntityStateError("deploy operation Deployment intent is missing")
    if deployment.status in {
        DeploymentStatus.PENDING.value,
        DeploymentStatus.APPLYING.value,
        DeploymentStatus.RECONCILING.value,
        DeploymentStatus.VERIFYING.value,
    }:
        deployment.status = DeploymentStatus.FAILED.value
        deployment.completed_at = datetime.now(UTC)
        deployment.failure_code = failure_code
        deployment.failure_message = failure_message
        session.flush()


def get_failed_release_deployment(
    session: Session, *, operation_id: UUID, lease_owner: str
) -> Deployment | None:
    """Find the original failed attempt even after the operation points at a rollback."""
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    return session.scalar(
        select(Deployment)
        .where(
            Deployment.operation_id == operation.id,
            Deployment.type == DeploymentType.RELEASE.value,
            Deployment.status == DeploymentStatus.FAILED.value,
        )
        .with_for_update()
    )


def prepare_automatic_rollback(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    failed_deployment_id: UUID,
    current_cr_release_id: UUID | None,
) -> RollbackState:
    """Persist at most one rollback intent while the deploy lease is held."""
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    failed = session.scalar(
        select(Deployment).where(Deployment.id == failed_deployment_id).with_for_update()
    )
    if (
        failed is None
        or failed.operation_id != operation.id
        or failed.type != DeploymentType.RELEASE.value
        or failed.status != DeploymentStatus.FAILED.value
    ):
        raise EntityStateError("original Deployment is not a failed release attempt")
    application = session.scalar(
        select(Application).where(Application.id == failed.application_id).with_for_update()
    )
    if application is None:
        raise EntityStateError("rollback Application is missing")
    rollback = session.scalar(
        select(Deployment)
        .where(Deployment.rollback_of_deployment_id == failed.id)
        .with_for_update()
    )
    target = (
        session.get(Release, failed.previous_release_id) if failed.previous_release_id else None
    )
    proven = (
        target is not None
        and session.scalar(
            select(Deployment.id)
            .where(
                Deployment.application_id == application.id,
                Deployment.desired_release_id == target.id,
                Deployment.status == DeploymentStatus.HEALTHY.value,
            )
            .limit(1)
        )
        is not None
    )
    eligible = (
        target is not None
        and proven
        and application.current_release_id == target.id
        and current_cr_release_id == failed.desired_release_id
    )
    if rollback is None and not eligible:
        return RollbackState(
            failed.id,
            failed.desired_release_id,
            failed.failure_code or "DEPLOYMENT_FAILED",
            failed.failure_message or "deployment failed",
            None,
            True,
        )
    if rollback is None:
        assert target is not None
        rollback = create_deployment(
            session,
            operation=operation,
            application=application,
            desired_release=target,
            previous_release=session.get(Release, failed.desired_release_id),
            deployment_type=DeploymentType.AUTOMATIC_ROLLBACK,
            rollback_of=failed,
        )
    elif operation.deployment_id != rollback.id:
        operation.deployment = rollback
        session.flush()
    if rollback.status == DeploymentStatus.PENDING.value:
        rollback.status = DeploymentStatus.APPLYING.value
        rollback.started_at = rollback.started_at or datetime.now(UTC)
        session.flush()
    restored = session.get(Release, rollback.desired_release_id)
    if restored is None:
        raise EntityStateError("rollback Release is missing")
    return RollbackState(
        failed.id,
        failed.desired_release_id,
        failed.failure_code or "DEPLOYMENT_FAILED",
        failed.failure_message or "deployment failed",
        ReleaseDeploymentIntent(
            rollback.id,
            application.name,
            restored.id,
            restored.image_reference,
            restored.port,
            restored.health_path,
            rollback.started_at or rollback.created_at,
            rollback.status,
        ),
        rollback_failure_code=rollback.failure_code,
        rollback_failure_message=rollback.failure_message,
    )


def assert_rollback_apply_allowed(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    current_release_id: UUID | None,
) -> None:
    operation = _leased_operation(session, operation_id=operation_id, lease_owner=lease_owner)
    rollback = session.get(Deployment, operation.deployment_id)
    if rollback is None or rollback.type != DeploymentType.AUTOMATIC_ROLLBACK.value:
        raise EntityStateError("operation has no automatic rollback intent")
    if current_release_id not in {rollback.previous_release_id, rollback.desired_release_id}:
        raise EntityStateError("Application desired Release was superseded during rollback")


def _claimable_operations() -> Select[tuple[Operation]]:
    return (
        select(Operation)
        .where(
            or_(
                Operation.status == OperationStatus.QUEUED.value,
                and_(
                    Operation.status == OperationStatus.RUNNING.value,
                    Operation.lease_expires_at <= func.now(),
                ),
            )
        )
        .order_by(Operation.created_at, Operation.id)
        .with_for_update(skip_locked=True)
    )


def _claim_operations(
    session: Session,
    *,
    lease_owner: str,
    limit: int,
    lease_duration: timedelta,
    now: datetime | None,
) -> list[tuple[Operation, bool]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    claimed_at = now or datetime.now(UTC)
    claimed: list[tuple[Operation, bool]] = []
    for operation in session.scalars(_claimable_operations().limit(limit)):
        reclaimed = operation.status == OperationStatus.RUNNING.value
        operation.status = OperationStatus.RUNNING.value
        operation.lease_owner = lease_owner
        operation.lease_expires_at = claimed_at + lease_duration
        operation.attempts += 1
        claimed.append((operation, reclaimed))
    session.flush()
    return claimed


def claim_pending_operations(
    session: Session,
    *,
    lease_owner: str,
    limit: int = 2,
    lease_duration: timedelta = OPERATION_LEASE_DURATION,
    now: datetime | None = None,
) -> list[Operation]:
    return [
        operation
        for operation, _reclaimed in _claim_operations(
            session,
            lease_owner=lease_owner,
            limit=limit,
            lease_duration=lease_duration,
            now=now,
        )
    ]


def claim_operation_work(
    session: Session,
    *,
    lease_owner: str,
    limit: int = 2,
    lease_duration: timedelta = OPERATION_LEASE_DURATION,
    now: datetime | None = None,
) -> list[OperationClaim]:
    return [
        OperationClaim(
            id=operation.id,
            type=operation.type,
            correlation_id=operation.correlation_id,
            attempt=operation.attempts,
            reclaimed=reclaimed,
        )
        for operation, reclaimed in _claim_operations(
            session,
            lease_owner=lease_owner,
            limit=limit,
            lease_duration=lease_duration,
            now=now,
        )
    ]


def renew_operation_lease(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    lease_duration: timedelta = OPERATION_LEASE_DURATION,
    now: datetime | None = None,
) -> bool:
    renewed_at = now or datetime.now(UTC)
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.RUNNING.value,
            Operation.lease_owner == lease_owner,
            Operation.lease_expires_at > renewed_at,
        )
        .values(lease_expires_at=renewed_at + lease_duration, updated_at=func.now())
    )
    return result.rowcount == 1


def release_operation_lease(session: Session, *, operation_id: UUID, lease_owner: str) -> bool:
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.RUNNING.value,
            Operation.lease_owner == lease_owner,
        )
        .values(
            status=OperationStatus.QUEUED.value,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=func.now(),
        )
    )
    return result.rowcount == 1


def count_queued_operations(session: Session) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Operation)
            .where(Operation.status == OperationStatus.QUEUED.value)
        )
        or 0
    )


def complete_operation(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    now: datetime | None = None,
) -> bool:
    completed_at = now or datetime.now(UTC)
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.RUNNING.value,
            Operation.lease_owner == lease_owner,
            Operation.lease_expires_at > completed_at,
        )
        .values(
            status=OperationStatus.SUCCEEDED.value,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=completed_at,
            failure_code=None,
            failure_message=None,
            failure_result=None,
            updated_at=func.now(),
        )
    )
    return result.rowcount == 1


def requeue_operation(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    failure_code: str,
    failure_message: str,
    failure_result: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    checked_at = now or datetime.now(UTC)
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.RUNNING.value,
            Operation.lease_owner == lease_owner,
            Operation.lease_expires_at > checked_at,
        )
        .values(
            status=OperationStatus.QUEUED.value,
            lease_owner=None,
            lease_expires_at=None,
            failure_code=failure_code,
            failure_message=failure_message,
            failure_result=failure_result,
            updated_at=func.now(),
        )
    )
    return result.rowcount == 1


def fail_operation(
    session: Session,
    *,
    operation_id: UUID,
    lease_owner: str,
    failure_code: str,
    failure_message: str,
    failure_result: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    completed_at = now or datetime.now(UTC)
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.RUNNING.value,
            Operation.lease_owner == lease_owner,
            Operation.lease_expires_at > completed_at,
        )
        .values(
            status=OperationStatus.FAILED.value,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=completed_at,
            failure_code=failure_code,
            failure_message=failure_message,
            failure_result=failure_result,
            updated_at=func.now(),
        )
    )
    return result.rowcount == 1
