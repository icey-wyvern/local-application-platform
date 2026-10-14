from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid1, uuid4

import pytest
from lap_platform_api.database import create_session_factory
from lap_platform_api.models import Application, Deployment, Operation, Release
from lap_platform_api.repositories import (
    IdempotencyConflictError,
    claim_pending_operations,
    create_application,
    create_build,
    create_deployment,
    create_idempotent_operation,
    create_release,
    get_application,
    get_application_by_name,
    release_operation_lease,
    renew_operation_lease,
    soft_delete_application,
)
from lap_platform_api.states import (
    BuildStatus,
    DeploymentType,
    OperationStatus,
    OperationType,
)
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def _operation(
    session: Session,
    *,
    repository: str | None = None,
    application_id: UUID | None = None,
) -> Operation:
    operation, created = create_idempotent_operation(
        session,
        operation_type=OperationType.DEPLOY,
        idempotency_key=uuid4(),
        request_hash="a" * 64,
        correlation_id=uuid4(),
        repository_url=repository,
        normalized_repository_url=repository,
        application_id=application_id,
    )
    assert created
    return operation


def _successful_release(session: Session, suffix: str = "one") -> tuple[Application, Release]:
    repository = f"https://example.com/test-{suffix}.git"
    application = create_application(
        session,
        name=f"test-{suffix}",
        repository_url=repository,
        normalized_repository_url=repository,
    )
    operation = _operation(session, repository=repository)
    operation.application = application
    build = create_build(
        session,
        operation=operation,
        application=application,
        repository_url=repository,
        normalized_repository_url=repository,
        requested_revision="main",
    )
    build.status = BuildStatus.SUCCEEDED.value
    build.resolved_commit = "b" * 40
    build.completed_at = datetime.now(UTC)
    session.flush()
    digest = "sha256:" + "c" * 64
    release = create_release(
        session,
        build=build,
        application=application,
        image_reference=f"registry.lab.test:5000/apps/test-{suffix}@{digest}",
        image_digest=digest,
        port=8080,
        health_path="/healthz",
    )
    operation.status = OperationStatus.SUCCEEDED.value
    operation.completed_at = datetime.now(UTC)
    session.flush()
    return application, release


def test_entity_relationships_and_repository_helpers(session: Session) -> None:
    application, release = _successful_release(session)
    operation = _operation(session, application_id=application.id)
    deployment = create_deployment(
        session,
        operation=operation,
        application=application,
        desired_release=release,
        deployment_type=DeploymentType.RELEASE,
    )
    session.flush()

    assert deployment.application is application
    assert deployment.desired_release is release
    assert release.build.release is release
    assert operation.deployment is deployment
    assert get_application(session, application.id) is application
    assert get_application_by_name(session, application.name) is application


def test_application_identity_is_permanent_after_soft_delete(session: Session) -> None:
    application = create_application(
        session,
        name="permanent-name",
        repository_url="https://example.com/permanent.git",
        normalized_repository_url="https://example.com/permanent.git",
    )
    soft_delete_application(session, application)
    assert get_application(session, application.id) is None
    assert get_application(session, application.id, include_deleted=True) is application

    with pytest.raises(IntegrityError), session.begin_nested():
        create_application(
            session,
            name="permanent-name",
            repository_url="https://example.com/other.git",
            normalized_repository_url="https://example.com/other.git",
        )

    with pytest.raises(DBAPIError, match="identity is immutable"), session.begin_nested():
        application.name = "changed-name"
        session.flush()
    session.refresh(application)

    with pytest.raises(DBAPIError, match="soft deleted"), session.begin_nested():
        session.delete(application)
        session.flush()


def test_release_is_immutable_and_requires_matching_digest(session: Session) -> None:
    _, release = _successful_release(session, "immutable")

    with pytest.raises(DBAPIError, match="immutable"), session.begin_nested():
        release.port = 9090
        session.flush()
    session.refresh(release)

    build = release.build
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            Release(
                application_id=release.application_id,
                build_id=uuid4(),
                git_commit=build.resolved_commit,
                image_reference="registry.lab.test/apps/bad@sha256:" + "d" * 64,
                image_digest="sha256:" + "d" * 64,
                port=80,
                health_path="healthz",
            )
        )
        session.flush()


def test_idempotency_replays_and_detects_conflict(session: Session) -> None:
    key = uuid4()
    correlation_id = uuid4()
    first, created = create_idempotent_operation(
        session,
        operation_type=OperationType.DELETE,
        idempotency_key=key,
        request_hash="e" * 64,
        correlation_id=correlation_id,
    )
    replay, replay_created = create_idempotent_operation(
        session,
        operation_type=OperationType.DELETE,
        idempotency_key=key,
        request_hash="e" * 64,
        correlation_id=correlation_id,
    )

    assert created is True
    assert replay_created is False
    assert replay.id == first.id

    with pytest.raises(IdempotencyConflictError):
        create_idempotent_operation(
            session,
            operation_type=OperationType.DELETE,
            idempotency_key=key,
            request_hash="f" * 64,
            correlation_id=correlation_id,
        )


def test_database_rejects_non_v4_idempotency_key(session: Session) -> None:
    operation = Operation(
        type=OperationType.DELETE.value,
        idempotency_key=uuid1(),
        request_hash="a" * 64,
        correlation_id=uuid4(),
    )
    with (
        pytest.raises(IntegrityError, match="ck_operations_idempotency_key_v4"),
        session.begin_nested(),
    ):
        session.add(operation)
        session.flush()


def test_skip_locked_claims_distinct_rows(database_engine: Engine) -> None:
    factory = create_session_factory(database_engine)
    with factory.begin() as setup:
        first = _operation(setup, repository=f"https://example.com/{uuid4()}.git")
        second = _operation(setup, repository=f"https://example.com/{uuid4()}.git")
        first_id, second_id = first.id, second.id

    worker_one = factory()
    worker_two = factory()
    try:
        first_claim = claim_pending_operations(worker_one, lease_owner="worker-one", limit=1)
        second_claim = claim_pending_operations(worker_two, lease_owner="worker-two", limit=1)
        assert len(first_claim) == len(second_claim) == 1
        assert {first_claim[0].id, second_claim[0].id} == {first_id, second_id}
        worker_one.commit()
        worker_two.commit()
    finally:
        worker_one.close()
        worker_two.close()


def test_lease_expiry_reclaim_renewal_and_release(database_engine: Engine) -> None:
    factory = create_session_factory(database_engine)
    with factory.begin() as setup:
        operation = _operation(setup, repository=f"https://example.com/{uuid4()}.git")
        operation_id = operation.id

    now = datetime.now(UTC)
    with factory.begin() as first_worker:
        claimed = claim_pending_operations(first_worker, lease_owner="worker-one", limit=1, now=now)
        assert claimed[0].id == operation_id

    with factory.begin() as expire:
        expire.execute(
            update(Operation)
            .where(Operation.id == operation_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )

    with factory.begin() as second_worker:
        reclaimed = claim_pending_operations(
            second_worker, lease_owner="worker-two", limit=1, now=now
        )
        assert reclaimed[0].id == operation_id
        assert reclaimed[0].attempts == 2
        assert renew_operation_lease(
            second_worker,
            operation_id=operation_id,
            lease_owner="worker-two",
            now=now,
        )
        assert not renew_operation_lease(
            second_worker,
            operation_id=operation_id,
            lease_owner="worker-one",
            now=now,
        )
        assert release_operation_lease(
            second_worker, operation_id=operation_id, lease_owner="worker-two"
        )

    with factory() as verify:
        stored = verify.scalar(select(Operation).where(Operation.id == operation_id))
        assert stored is not None
        assert stored.status == OperationStatus.QUEUED.value
        assert stored.lease_owner is None


def test_foreign_keys_and_active_deployment_constraint(session: Session) -> None:
    application, release = _successful_release(session, "constraints")
    first_operation = _operation(session, application_id=application.id)
    create_deployment(
        session,
        operation=first_operation,
        application=application,
        desired_release=release,
        deployment_type=DeploymentType.RELEASE,
    )

    second_operation = _operation(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Deployment(
                operation_id=second_operation.id,
                application_id=application.id,
                desired_release_id=release.id,
                type=DeploymentType.RELEASE.value,
            )
        )
        session.flush()

    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Deployment(
                operation_id=uuid4(),
                application_id=application.id,
                desired_release_id=release.id,
                type=DeploymentType.MANUAL_ROLLBACK.value,
            )
        )
        session.flush()
