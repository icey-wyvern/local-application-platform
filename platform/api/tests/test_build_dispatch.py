from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from lap_platform_api.build_agent import (
    ApplicationMetadata,
    BuildAgentError,
    BuildAgentErrorDetail,
    BuildRequest,
    BuildResult,
    VulnerabilitySummary,
)
from lap_platform_api.build_dispatch import DeployBuildHandler, ManualRollbackHandler
from lap_platform_api.database import create_session_factory
from lap_platform_api.kubernetes import (
    ApplicationApplyResult,
    ApplicationResource,
    DesiredApplication,
    KubernetesApplicationRolloutError,
    KubernetesApplicationTransientError,
)
from lap_platform_api.metrics import ApiMetrics
from lap_platform_api.models import Application, Build, Deployment, Operation, Release
from lap_platform_api.repositories import (
    claim_operation_work,
    complete_operation,
    create_build,
    create_idempotent_operation,
    fail_operation,
    request_manual_rollback,
    requeue_operation,
)
from lap_platform_api.scheduler import (
    NoOpOperationHandler,
    OperationExecutionContext,
    OperationHandler,
    OperationOutcome,
    OperationScheduler,
)
from lap_platform_api.states import (
    BuildStatus,
    DeploymentStatus,
    OperationStatus,
    OperationType,
)
from sqlalchemy import Engine, func, select, update

pytestmark = pytest.mark.integration
API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def migrated_database(database_engine: Engine) -> None:
    with database_engine.connect() as connection:
        configuration = Config(API_ROOT / "alembic.ini")
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "head")
        connection.commit()


@pytest.fixture(autouse=True)
def isolate_queue(database_engine: Engine, migrated_database: None) -> None:
    factory = create_session_factory(database_engine)
    with factory.begin() as session:
        session.execute(
            update(Operation)
            .where(Operation.status.in_(["queued", "running"]))
            .values(
                status=OperationStatus.FAILED.value,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=datetime.now(UTC),
                failure_code="TEST_QUEUE_ISOLATION",
                failure_message="isolated between build dispatch tests",
            )
        )


def _result(
    request: BuildRequest,
    name: str,
    *,
    digest_character: str = "d",
    commit_character: str = "c",
) -> BuildResult:
    digest = "sha256:" + digest_character * 64
    return BuildResult(
        buildId=request.buildId,
        commit=commit_character * 40,
        imageReference=f"registry.lab.test:5000/platform/{name}@{digest}",
        imageDigest=digest,
        application=ApplicationMetadata(name=name, port=8080, healthPath="/healthz"),
        vulnerabilities=VulnerabilitySummary(
            threshold="CRITICAL",
            counts={"UNKNOWN": 0, "LOW": 1, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0},
            blocked=0,
        ),
    )


class SuccessfulAgent:
    def __init__(
        self,
        name: str,
        *,
        delay: float = 0,
        digest_character: str = "d",
        commit_character: str = "c",
    ) -> None:
        self.name = name
        self.delay = delay
        self.digest_character = digest_character
        self.commit_character = commit_character
        self.calls: Counter[UUID] = Counter()
        self.active = 0
        self.maximum_active = 0

    async def submit_build(self, request: BuildRequest) -> BuildResult:
        self.calls[request.buildId] += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return _result(
                request,
                self.name,
                digest_character=self.digest_character,
                commit_character=self.commit_character,
            )
        finally:
            self.active -= 1


class FailingAgent:
    def __init__(self, error: BuildAgentError) -> None:
        self.error = error

    async def submit_build(self, _request: BuildRequest) -> BuildResult:
        raise self.error


class BusyThenSuccessfulAgent(SuccessfulAgent):
    async def submit_build(self, request: BuildRequest) -> BuildResult:
        if not self.calls[request.buildId]:
            self.calls[request.buildId] += 1
            raise BuildAgentError(
                409,
                BuildAgentErrorDetail(
                    code="build_in_progress",
                    message="build host is busy",
                    retryable=True,
                ),
            )
        return await super().submit_build(request)


class RecordingApplicationClient:
    def __init__(self, *, transient_failures: int = 0) -> None:
        self.resources: dict[str, ApplicationResource] = {}
        self.actions: list[str] = []
        self.transient_failures = transient_failures

    async def get(self, name: str) -> ApplicationResource | None:
        return self.resources.get(name)

    async def apply(
        self,
        desired: DesiredApplication,
        current: ApplicationResource | None,
    ) -> ApplicationApplyResult:
        if self.transient_failures:
            self.transient_failures -= 1
            raise KubernetesApplicationTransientError("temporary Kubernetes failure")
        action: Literal["created", "updated", "unchanged"] = (
            "created" if current is None else "updated"
        )
        if current is not None and current.spec == desired.spec():
            action = "unchanged"
        generation = 1 if current is None else current.metadata.generation + (action == "updated")
        resource = ApplicationResource.model_validate({
            **desired.resource_document(),
            "metadata": {
                "name": desired.name,
                "generation": generation,
                "resourceVersion": str(generation),
            },
        })
        self.resources[desired.name] = resource
        self.actions.append(action)
        return ApplicationApplyResult(resource, action)

    async def wait_until_observed(
        self,
        desired: DesiredApplication,
        generation: int,
        lease_lost: asyncio.Event,
    ) -> ApplicationResource:
        if lease_lost.is_set():
            raise KubernetesApplicationTransientError("operation lease was lost")
        resource = self.resources[desired.name]
        observed = ApplicationResource.model_validate({
            **resource.model_dump(mode="json", by_alias=True, exclude_none=True),
            "status": {
                "observedGeneration": generation,
                "desiredImage": desired.image,
                "conditions": [
                    {
                        "type": "Reconciled",
                        "status": "True",
                        "reason": "RuntimeResourcesReconciled",
                        "observedGeneration": generation,
                    }
                ],
            },
        })
        self.resources[desired.name] = observed
        return observed

    async def wait_until_ready(
        self,
        desired: DesiredApplication,
        generation: int,
        lease_lost: asyncio.Event,
        _timeout_seconds: float,
    ) -> ApplicationResource:
        if lease_lost.is_set():
            raise KubernetesApplicationTransientError("operation lease was lost")
        resource = self.resources[desired.name]
        ready = ApplicationResource.model_validate({
            **resource.model_dump(mode="json", by_alias=True, exclude_none=True),
            "status": {
                "observedGeneration": generation,
                "activeReleaseId": str(desired.release_id),
                "desiredImage": desired.image,
                "currentImage": desired.image,
                "readyReplicas": 2,
                "phase": "Ready",
                "conditions": [
                    {
                        "type": condition,
                        "status": "True",
                        "reason": reason,
                        "observedGeneration": generation,
                    }
                    for condition, reason in (
                        ("Reconciled", "RuntimeResourcesReconciled"),
                        ("RuntimeReady", "WorkloadAndServiceReady"),
                        ("RouteReady", "RouteAccepted"),
                        ("Healthy", "ExternalHealthCheckSucceeded"),
                        ("Available", "ApplicationHealthy"),
                    )
                ],
            },
        })
        self.resources[desired.name] = ready
        return ready

    async def verify_external_health(
        self,
        _desired: DesiredApplication,
        lease_lost: asyncio.Event,
        _timeout_seconds: float,
    ) -> None:
        if lease_lost.is_set():
            raise KubernetesApplicationTransientError("operation lease was lost")


class FailedAcceptanceClient(RecordingApplicationClient):
    def __init__(self, code: str, stage: str) -> None:
        super().__init__()
        self.code = code
        self.stage = stage

    async def wait_until_ready(
        self,
        _desired: DesiredApplication,
        _generation: int,
        _lease_lost: asyncio.Event,
        _timeout_seconds: float,
    ) -> ApplicationResource:
        raise KubernetesApplicationRolloutError(
            self.code, "fixture deployment acceptance failure", self.stage
        )


class ReleaseAwareAcceptanceClient(RecordingApplicationClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed_releases: set[UUID] = set()
        self.healthy_release: UUID | None = None
        self.fail_new = False
        self.fail_restored = False

    async def verify_external_health(
        self, desired: DesiredApplication, lease_lost: asyncio.Event, _timeout_seconds: float
    ) -> None:
        if (
            desired.release_id in self.failed_releases
            or (self.fail_new and desired.release_id != self.healthy_release)
            or (self.fail_restored and desired.release_id == self.healthy_release)
        ):
            raise KubernetesApplicationRolloutError(
                "EXTERNAL_HTTPS_HEALTH_FAILED", "fixture HTTPS failure", "external_https"
            )
        await super().verify_external_health(desired, lease_lost, _timeout_seconds)


def _next_deploy(engine: Engine, application_name: str) -> tuple[UUID, UUID]:
    factory = create_session_factory(engine)
    with factory.begin() as session:
        application = session.scalar(
            select(Application).where(Application.name == application_name)
        )
        assert application is not None
        operation, created = create_idempotent_operation(
            session,
            operation_type=OperationType.DEPLOY,
            idempotency_key=uuid4(),
            request_hash=uuid4().hex * 2,
            correlation_id=uuid4(),
            repository_url=application.repository_url,
            normalized_repository_url=application.normalized_repository_url,
            git_revision="refs/heads/main",
            application_id=application.id,
        )
        assert created
        build = create_build(
            session,
            operation=operation,
            repository_url=application.repository_url,
            normalized_repository_url=application.normalized_repository_url,
            requested_revision="refs/heads/main",
            application=application,
        )
        return operation.id, build.id


def _create_deploy(engine: Engine) -> tuple[UUID, UUID, str]:
    suffix = uuid4().hex[:10]
    repository = f"https://example.com/build-dispatch-{suffix}.git"
    factory = create_session_factory(engine)
    with factory.begin() as session:
        operation, created = create_idempotent_operation(
            session,
            operation_type=OperationType.DEPLOY,
            idempotency_key=uuid4(),
            request_hash=uuid4().hex * 2,
            correlation_id=uuid4(),
            repository_url=repository,
            normalized_repository_url=repository,
            git_revision="refs/heads/main",
        )
        assert created
        build = create_build(
            session,
            operation=operation,
            repository_url=repository,
            normalized_repository_url=repository,
            requested_revision="refs/heads/main",
        )
        return operation.id, build.id, f"dispatch-{suffix}"


def _claim_context(engine: Engine, operation_id: UUID, owner: str) -> OperationExecutionContext:
    factory = create_session_factory(engine)
    with factory.begin() as session:
        claims = claim_operation_work(session, lease_owner=owner, limit=1)
    assert claims[0].id == operation_id
    claim = claims[0]
    return OperationExecutionContext(
        operation_id=claim.id,
        operation_type=claim.type,
        correlation_id=claim.correlation_id,
        attempt=claim.attempt,
        lease_owner=owner,
        lease_lost=asyncio.Event(),
    )


def test_success_persists_agent_commit_digest_application_and_release(
    database_engine: Engine,
) -> None:
    operation_id, build_id, name = _create_deploy(database_engine)
    context = _claim_context(database_engine, operation_id, "success-owner")
    agent = SuccessfulAgent(name)
    application_client = RecordingApplicationClient()
    handler = DeployBuildHandler(
        create_session_factory(database_engine),
        agent,
        ApiMetrics(),
        application_client=application_client,
    )

    outcome = asyncio.run(handler.execute(context))
    assert outcome.outcome is OperationOutcome.SUCCEEDED

    factory = create_session_factory(database_engine)
    with factory() as session:
        build = session.get(Build, build_id)
        application = session.scalar(select(Application).where(Application.name == name))
        release = session.scalar(select(Release).where(Release.build_id == build_id))
        deployment = session.scalar(
            select(Deployment).where(Deployment.operation_id == operation_id)
        )
        operation = session.get(Operation, operation_id)
        assert (
            build is not None
            and application is not None
            and release is not None
            and operation is not None
            and deployment is not None
        )
        assert build.status == BuildStatus.SUCCEEDED.value
        assert build.resolved_commit == "c" * 40
        assert build.application_id == application.id == operation.application_id
        assert release.git_commit == build.resolved_commit
        assert release.image_digest == "sha256:" + "d" * 64
        assert release.image_reference.endswith(f"@{release.image_digest}")
        assert release.port == 8080 and release.health_path == "/healthz"
        assert deployment.desired_release_id == release.id
        assert deployment.status == DeploymentStatus.HEALTHY.value
        assert deployment.completed_at is not None
        assert application.current_release_id == release.id
        assert operation.deployment_id == deployment.id
    resource = application_client.resources[name]
    assert resource.spec.model_dump(mode="json", by_alias=True) == {
        "releaseId": str(release.id),
        "image": release.image_reference,
        "port": release.port,
        "healthPath": release.health_path,
        "replicas": 2,
    }


def test_runtime_acceptance_failure_persists_deployment_failure(
    database_engine: Engine,
) -> None:
    operation_id, build_id, name = _create_deploy(database_engine)
    context = _claim_context(database_engine, operation_id, "failed-rollout-owner")
    metrics = ApiMetrics()
    handler = DeployBuildHandler(
        create_session_factory(database_engine),
        SuccessfulAgent(name),
        metrics,
        application_client=FailedAcceptanceClient("EXTERNAL_HTTPS_HEALTH_FAILED", "external_https"),
    )

    outcome = asyncio.run(handler.execute(context))
    assert outcome.outcome is OperationOutcome.FAILED
    assert outcome.failure_code == "ROLLBACK_TARGET_MISSING"
    assert outcome.failure_result is not None
    assert outcome.failure_result["rollbackStatus"] == "unavailable"

    factory = create_session_factory(database_engine)
    with factory() as session:
        build = session.get(Build, build_id)
        operation = session.get(Operation, operation_id)
        deployment = session.scalar(
            select(Deployment).where(Deployment.operation_id == operation_id)
        )
        assert build is not None and build.status == BuildStatus.SUCCEEDED.value
        assert operation is not None and operation.deployment_id is not None
        assert deployment is not None
        assert deployment.status == DeploymentStatus.FAILED.value
        assert deployment.failure_code == "EXTERNAL_HTTPS_HEALTH_FAILED"
        assert deployment.completed_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(Deployment)
                .where(Deployment.operation_id == operation_id)
            )
            == 1
        )
    assert (
        metrics.registry.get_sample_value(
            "lap_platform_api_deployment_acceptance_stages_total",
            {"stage": "external_https", "outcome": "failed"},
        )
        == 1
    )
    assert (
        metrics.registry.get_sample_value(
            "lap_platform_api_rollbacks_total", {"outcome": "unavailable"}
        )
        == 1
    )


@pytest.mark.parametrize("restore_fails", [False, True])
def test_automatic_rollback_records_separate_result_and_never_recurses(
    database_engine: Engine, restore_fails: bool
) -> None:
    first_id, _, name = _create_deploy(database_engine)
    factory = create_session_factory(database_engine)
    client = ReleaseAwareAcceptanceClient()
    metrics = ApiMetrics()
    first_context = _claim_context(database_engine, first_id, "rollback-first")
    first_handler = DeployBuildHandler(
        factory, SuccessfulAgent(name), metrics, application_client=client
    )
    assert asyncio.run(first_handler.execute(first_context)).outcome is OperationOutcome.SUCCEEDED
    with factory.begin() as session:
        assert complete_operation(
            session, operation_id=first_id, lease_owner=first_context.lease_owner
        )
        application = session.scalar(select(Application).where(Application.name == name))
        assert application is not None
        release_a = application.current_release_id
    assert release_a is not None
    client.healthy_release = release_a
    client.fail_new = True
    client.fail_restored = restore_fails

    second_id, second_build = _next_deploy(database_engine, name)
    second_context = _claim_context(database_engine, second_id, "rollback-second")
    handler = DeployBuildHandler(
        factory,
        SuccessfulAgent(name, digest_character="e"),
        metrics,
        application_client=client,
    )
    outcome = asyncio.run(handler.execute(second_context))
    assert outcome.outcome is OperationOutcome.FAILED
    assert outcome.failure_result is not None
    assert outcome.failure_result["rollbackStatus"] == ("failed" if restore_fails else "succeeded")
    replay = asyncio.run(handler.execute(second_context))
    assert replay.failure_result == outcome.failure_result or (
        restore_fails
        and replay.failure_result is not None
        and replay.failure_result["rollbackStatus"] == "failed"
    )
    with factory.begin() as session:
        assert fail_operation(
            session,
            operation_id=second_id,
            lease_owner=second_context.lease_owner,
            failure_code=outcome.failure_code or "DEPLOYMENT_FAILED",
            failure_message=outcome.failure_message or "deployment failed",
            failure_result=outcome.failure_result,
        )
    with factory() as session:
        release_b = session.scalar(select(Release).where(Release.build_id == second_build))
        application = session.scalar(select(Application).where(Application.name == name))
        deployments = list(
            session.scalars(select(Deployment).where(Deployment.operation_id == second_id))
        )
        assert release_b is not None and application is not None
        assert len(deployments) == 2
        failed = next(item for item in deployments if item.type == "release")
        rollback = next(item for item in deployments if item.type == "automatic_rollback")
        assert failed.status == "failed" and failed.desired_release_id == release_b.id
        assert rollback.rollback_of_deployment_id == failed.id
        assert rollback.desired_release_id == release_a
        assert rollback.previous_release_id == release_b.id
        assert rollback.status == ("failed" if restore_fails else "healthy")
        assert application.current_release_id == release_a
    assert client.resources[name].spec.release_id == release_a
    assert client.actions == ["created", "updated", "updated"]
    assert (
        metrics.registry.get_sample_value(
            "lap_platform_api_rollbacks_total",
            {"outcome": "failed" if restore_fails else "succeeded"},
        )
        == 1
    )
    assert (
        metrics.registry.get_sample_value(
            "lap_platform_api_rollback_acceptance_stages_total",
            {"stage": "external_https", "outcome": "failed" if restore_fails else "succeeded"},
        )
        == 1
    )


def test_rollback_reclaim_keeps_one_intent_and_blocks_stale_owner(database_engine: Engine) -> None:
    first_id, _, name = _create_deploy(database_engine)
    factory = create_session_factory(database_engine)

    class LeaseStealingRollbackClient(ReleaseAwareAcceptanceClient):
        def __init__(self) -> None:
            super().__init__()
            self.target_operation: UUID | None = None
            self.new_release_reads = 0

        async def get(self, application_name: str) -> ApplicationResource | None:
            resource = await super().get(application_name)
            if (
                self.target_operation is not None
                and resource is not None
                and resource.spec.release_id != self.healthy_release
            ):
                self.new_release_reads += 1
                if self.new_release_reads == 2:
                    with factory.begin() as session:
                        session.execute(
                            update(Operation)
                            .where(Operation.id == self.target_operation)
                            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                        )
            return resource

    client = LeaseStealingRollbackClient()
    first_context = _claim_context(database_engine, first_id, "reclaim-first")
    assert (
        asyncio.run(
            DeployBuildHandler(
                factory, SuccessfulAgent(name), ApiMetrics(), application_client=client
            ).execute(first_context)
        ).outcome
        is OperationOutcome.SUCCEEDED
    )
    with factory.begin() as session:
        assert complete_operation(
            session, operation_id=first_id, lease_owner=first_context.lease_owner
        )
        application = session.scalar(select(Application).where(Application.name == name))
        assert application is not None
        client.healthy_release = application.current_release_id
    client.fail_new = True
    second_id, _ = _next_deploy(database_engine, name)
    client.target_operation = second_id
    stale_context = _claim_context(database_engine, second_id, "reclaim-stale")
    handler = DeployBuildHandler(
        factory,
        SuccessfulAgent(name, digest_character="e"),
        ApiMetrics(),
        application_client=client,
    )
    stale_result = asyncio.run(handler.execute(stale_context))
    assert stale_result.outcome is OperationOutcome.RETRY
    assert stale_context.lease_lost.is_set()
    assert client.actions == ["created", "updated"]

    recovered_context = _claim_context(database_engine, second_id, "reclaim-replacement")
    recovered = asyncio.run(handler.execute(recovered_context))
    assert recovered.outcome is OperationOutcome.FAILED
    assert recovered.failure_result is not None
    assert recovered.failure_result["rollbackStatus"] == "succeeded"
    with factory() as session:
        deployments = list(
            session.scalars(select(Deployment).where(Deployment.operation_id == second_id))
        )
        assert len(deployments) == 2
        assert sum(item.type == "automatic_rollback" for item in deployments) == 1
        application = session.scalar(select(Application).where(Application.name == name))
        assert application is not None
        assert application.current_release_id == client.healthy_release
    assert client.actions == ["created", "updated", "updated"]


@pytest.mark.parametrize(
    ("agent_code", "expected_status"),
    [
        ("vulnerability_policy_rejected", BuildStatus.REJECTED.value),
        ("build_failed", BuildStatus.FAILED.value),
    ],
)
def test_terminal_agent_failure_creates_no_release(
    database_engine: Engine, agent_code: str, expected_status: str
) -> None:
    operation_id, build_id, _name = _create_deploy(database_engine)
    context = _claim_context(database_engine, operation_id, f"terminal-{agent_code}")
    handler = DeployBuildHandler(
        create_session_factory(database_engine),
        FailingAgent(
            BuildAgentError(
                422,
                BuildAgentErrorDetail(
                    code=agent_code,
                    message="fixture terminal failure",
                    stage="scan" if "vulnerability" in agent_code else "build",
                    retryable=False,
                ),
            )
        ),
        ApiMetrics(),
        application_client=RecordingApplicationClient(),
    )

    outcome = asyncio.run(handler.execute(context))
    assert outcome.outcome is OperationOutcome.FAILED
    factory = create_session_factory(database_engine)
    with factory() as session:
        build = session.get(Build, build_id)
        assert build is not None and build.status == expected_status
        assert build.failure_code == agent_code.upper()
        release_count = session.scalar(
            select(func.count()).select_from(Release).where(Release.build_id == build_id)
        )
        assert release_count == 0


def _handlers(handler: OperationHandler) -> dict[str, OperationHandler]:
    noop = NoOpOperationHandler()
    return {
        OperationType.DEPLOY.value: handler,
        OperationType.DELETE.value: noop,
        OperationType.ROLLBACK.value: noop,
    }


async def _wait_for_terminal(engine: Engine, operation_id: UUID, timeout: float = 5) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    factory = create_session_factory(engine)
    while asyncio.get_running_loop().time() < deadline:
        with factory() as session:
            status = session.scalar(select(Operation.status).where(Operation.id == operation_id))
        if status in {OperationStatus.SUCCEEDED.value, OperationStatus.FAILED.value}:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("operation did not become terminal")


def test_busy_agent_retries_and_two_schedulers_dispatch_each_build_once(
    database_engine: Engine,
) -> None:
    operation_id, build_id, name = _create_deploy(database_engine)
    agent = BusyThenSuccessfulAgent(name, delay=0.1)
    metrics = ApiMetrics()
    application_client = RecordingApplicationClient()
    handler = DeployBuildHandler(
        create_session_factory(database_engine),
        agent,
        metrics,
        application_client=application_client,
    )

    async def exercise() -> None:
        first = OperationScheduler(
            create_session_factory(database_engine),
            _handlers(handler),
            metrics,
            lease_owner="api-replica-one",
            poll_interval_seconds=0.01,
            retry_delay_seconds=0.01,
            lease_duration=timedelta(seconds=1),
            renewal_interval=timedelta(seconds=0.1),
        )
        second = OperationScheduler(
            create_session_factory(database_engine),
            _handlers(handler),
            metrics,
            lease_owner="api-replica-two",
            poll_interval_seconds=0.01,
            retry_delay_seconds=0.01,
            lease_duration=timedelta(seconds=1),
            renewal_interval=timedelta(seconds=0.1),
        )
        await first.start()
        await second.start()
        try:
            await _wait_for_terminal(database_engine, operation_id)
        finally:
            await first.stop()
            await second.stop()

    asyncio.run(exercise())
    factory = create_session_factory(database_engine)
    with factory() as session:
        operation = session.get(Operation, operation_id)
        assert operation is not None
        assert operation.status == OperationStatus.SUCCEEDED.value
        assert operation.attempts == 2
        release_count = session.scalar(
            select(func.count()).select_from(Release).where(Release.build_id == build_id)
        )
        assert release_count == 1
    assert agent.calls[build_id] == 2
    assert agent.maximum_active == 1
    assert application_client.actions == ["created"]
    assert (
        metrics.registry.get_sample_value(
            "lap_platform_api_build_results_total",
            {"outcome": "retry", "failure_kind": "infrastructure"},
        )
        == 1
    )


def test_transient_kubernetes_failure_reuses_release_and_deployment(
    database_engine: Engine,
) -> None:
    operation_id, build_id, name = _create_deploy(database_engine)
    factory = create_session_factory(database_engine)
    agent = SuccessfulAgent(name)
    application_client = RecordingApplicationClient(transient_failures=1)
    handler = DeployBuildHandler(
        factory,
        agent,
        ApiMetrics(),
        application_client=application_client,
    )

    first = _claim_context(database_engine, operation_id, "first-owner")
    first_outcome = asyncio.run(handler.execute(first))
    assert first_outcome.outcome is OperationOutcome.RETRY
    with factory.begin() as session:
        assert requeue_operation(
            session,
            operation_id=operation_id,
            lease_owner=first.lease_owner,
            failure_code=first_outcome.failure_code or "TEST_RETRY",
            failure_message=first_outcome.failure_message or "test retry",
        )

    second = _claim_context(database_engine, operation_id, "second-owner")
    second_outcome = asyncio.run(handler.execute(second))
    assert second_outcome.outcome is OperationOutcome.SUCCEEDED

    with factory() as session:
        release_count = session.scalar(
            select(func.count()).select_from(Release).where(Release.build_id == build_id)
        )
        deployment_count = session.scalar(
            select(func.count())
            .select_from(Deployment)
            .where(Deployment.operation_id == operation_id)
        )
        assert release_count == 1
        assert deployment_count == 1
    assert agent.calls[build_id] == 1
    assert application_client.actions == ["created"]


def test_new_release_updates_existing_application_cr_once(
    database_engine: Engine,
) -> None:
    first_operation_id, _first_build_id, name = _create_deploy(database_engine)
    factory = create_session_factory(database_engine)
    application_client = RecordingApplicationClient()
    first_handler = DeployBuildHandler(
        factory,
        SuccessfulAgent(name),
        ApiMetrics(),
        application_client=application_client,
    )
    first_context = _claim_context(database_engine, first_operation_id, "first-release-owner")
    assert asyncio.run(first_handler.execute(first_context)).outcome is OperationOutcome.SUCCEEDED

    with factory.begin() as session:
        first_operation = session.get(Operation, first_operation_id)
        assert first_operation is not None and first_operation.deployment_id is not None
        first_deployment = session.get(Deployment, first_operation.deployment_id)
        assert first_deployment is not None
        assert first_operation.application is not None
        first_deployment.status = DeploymentStatus.HEALTHY.value
        first_deployment.completed_at = datetime.now(UTC)
        first_operation.application.current_release_id = first_deployment.desired_release_id
        assert complete_operation(
            session,
            operation_id=first_operation_id,
            lease_owner=first_context.lease_owner,
        )
        repository = first_operation.normalized_repository_url
        assert repository is not None
        second_operation, created = create_idempotent_operation(
            session,
            operation_type=OperationType.DEPLOY,
            idempotency_key=uuid4(),
            request_hash=uuid4().hex * 2,
            correlation_id=uuid4(),
            repository_url=repository,
            normalized_repository_url=repository,
            git_revision="refs/heads/main",
        )
        assert created
        second_build = create_build(
            session,
            operation=second_operation,
            repository_url=repository,
            normalized_repository_url=repository,
            requested_revision="refs/heads/main",
        )
        second_operation_id = second_operation.id
        second_build_id = second_build.id

    second_handler = DeployBuildHandler(
        factory,
        SuccessfulAgent(name, digest_character="e", commit_character="e"),
        ApiMetrics(),
        application_client=application_client,
    )
    second_context = _claim_context(database_engine, second_operation_id, "second-release-owner")
    assert asyncio.run(second_handler.execute(second_context)).outcome is OperationOutcome.SUCCEEDED

    with factory() as session:
        second_release = session.scalar(select(Release).where(Release.build_id == second_build_id))
        assert second_release is not None
        assert application_client.resources[name].spec.release_id == second_release.id
        assert application_client.resources[name].metadata.generation == 2
        deployment_count = session.scalar(
            select(func.count())
            .select_from(Deployment)
            .where(Deployment.application_id == second_release.application_id)
        )
        assert deployment_count == 2
    assert application_client.actions == ["created", "updated"]


def test_manual_rollback_reuses_historical_release_without_a_build(database_engine: Engine) -> None:
    first_id, _, name = _create_deploy(database_engine)
    factory = create_session_factory(database_engine)
    client = RecordingApplicationClient()
    agent = SuccessfulAgent(name)
    first = _claim_context(database_engine, first_id, "manual-first")
    first_handler = DeployBuildHandler(factory, agent, ApiMetrics(), application_client=client)
    assert asyncio.run(first_handler.execute(first)).outcome is OperationOutcome.SUCCEEDED
    with factory.begin() as session:
        first_operation = session.get(Operation, first_id)
        assert first_operation is not None and first_operation.deployment_id is not None
        earlier = session.get(Deployment, first_operation.deployment_id)
        assert earlier is not None
        earlier_release_id = earlier.desired_release_id
        assert complete_operation(session, operation_id=first_id, lease_owner=first.lease_owner)

    second_id, _ = _next_deploy(database_engine, name)
    second = _claim_context(database_engine, second_id, "manual-second")
    second_handler = DeployBuildHandler(
        factory,
        SuccessfulAgent(name, digest_character="e", commit_character="e"),
        ApiMetrics(),
        application_client=client,
    )
    assert asyncio.run(second_handler.execute(second)).outcome is OperationOutcome.SUCCEEDED
    with factory.begin() as session:
        assert complete_operation(session, operation_id=second_id, lease_owner=second.lease_owner)
        application = session.scalar(select(Application).where(Application.name == name))
        assert application is not None and application.current_release_id != earlier_release_id
        newer_release_id = application.current_release_id
        release_count = session.scalar(select(func.count()).select_from(Release))
        build_count = session.scalar(select(func.count()).select_from(Build))
        rollback = request_manual_rollback(
            session,
            application_name=name,
            release_id=earlier_release_id,
            idempotency_key=uuid4(),
            request_hash="f" * 64,
            correlation_id=uuid4(),
        )
        rollback_id = rollback.id

    context = _claim_context(database_engine, rollback_id, "manual-rollback")
    handler = ManualRollbackHandler(factory, agent, ApiMetrics(), application_client=client)
    assert asyncio.run(handler.execute(context)).outcome is OperationOutcome.SUCCEEDED
    with factory() as session:
        application = session.scalar(select(Application).where(Application.name == name))
        stored_rollback = session.get(Operation, rollback_id)
        assert application is not None and stored_rollback is not None
        assert application.current_release_id == earlier_release_id
        assert stored_rollback.build_id is None and stored_rollback.deployment_id is not None
        deployment = session.get(Deployment, stored_rollback.deployment_id)
        assert deployment is not None
        assert deployment.type == "manual_rollback"
        assert deployment.status == DeploymentStatus.HEALTHY.value
        assert deployment.previous_release_id == newer_release_id
        assert session.scalar(select(func.count()).select_from(Release)) == release_count
        assert session.scalar(select(func.count()).select_from(Build)) == build_count
    assert client.resources[name].spec.release_id == earlier_release_id
    assert client.resources[name].metadata.generation == 3
    assert agent.calls.total() == 1


def test_stale_worker_cannot_persist_success_and_reclaimed_build_reuses_identity(
    database_engine: Engine,
) -> None:
    operation_id, build_id, name = _create_deploy(database_engine)
    context = _claim_context(database_engine, operation_id, "old-api-pod")
    factory = create_session_factory(database_engine)

    class LeaseStealingAgent(SuccessfulAgent):
        async def submit_build(self, request: BuildRequest) -> BuildResult:
            result = await super().submit_build(request)
            with factory.begin() as session:
                session.execute(
                    update(Operation)
                    .where(Operation.id == operation_id)
                    .values(
                        lease_owner="dead-api-pod",
                        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    )
                )
            return result

    stale_handler = DeployBuildHandler(
        factory,
        LeaseStealingAgent(name),
        ApiMetrics(),
        application_client=RecordingApplicationClient(),
    )
    stale_outcome = asyncio.run(stale_handler.execute(context))
    assert stale_outcome.outcome is OperationOutcome.RETRY
    assert context.lease_lost.is_set()
    with factory() as session:
        release_count = session.scalar(
            select(func.count()).select_from(Release).where(Release.build_id == build_id)
        )
        assert release_count == 0

    recovery_agent = SuccessfulAgent(name)
    recovery = OperationScheduler(
        factory,
        _handlers(
            DeployBuildHandler(
                factory,
                recovery_agent,
                ApiMetrics(),
                application_client=RecordingApplicationClient(),
            )
        ),
        ApiMetrics(),
        lease_owner="replacement-api-pod",
        poll_interval_seconds=0.01,
        retry_delay_seconds=0,
        lease_duration=timedelta(seconds=1),
        renewal_interval=timedelta(seconds=0.1),
    )

    async def recover() -> None:
        await recovery.start()
        try:
            await _wait_for_terminal(database_engine, operation_id)
        finally:
            await recovery.stop()

    asyncio.run(recover())
    with factory() as session:
        operation = session.get(Operation, operation_id)
        assert operation is not None and operation.attempts == 2
        assert operation.status == OperationStatus.SUCCEEDED.value
        release_count = session.scalar(
            select(func.count()).select_from(Release).where(Release.build_id == build_id)
        )
        assert release_count == 1
    assert recovery_agent.calls[build_id] == 1


def test_lease_loss_before_application_apply_prevents_stale_cr_write(
    database_engine: Engine,
) -> None:
    operation_id, build_id, name = _create_deploy(database_engine)
    factory = create_session_factory(database_engine)

    class LeaseStealingApplicationClient(RecordingApplicationClient):
        async def get(self, application_name: str) -> ApplicationResource | None:
            with factory.begin() as session:
                session.execute(
                    update(Operation)
                    .where(Operation.id == operation_id)
                    .values(
                        lease_owner="replacement-owner",
                        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    )
                )
            return await super().get(application_name)

    stale_client = LeaseStealingApplicationClient()
    stale_handler = DeployBuildHandler(
        factory,
        SuccessfulAgent(name),
        ApiMetrics(),
        application_client=stale_client,
    )
    stale_context = _claim_context(database_engine, operation_id, "stale-owner")
    stale_outcome = asyncio.run(stale_handler.execute(stale_context))
    assert stale_outcome.outcome is OperationOutcome.RETRY
    assert stale_context.lease_lost.is_set()
    assert stale_client.actions == []

    recovery_client = RecordingApplicationClient()
    recovery_handler = DeployBuildHandler(
        factory,
        SuccessfulAgent(name),
        ApiMetrics(),
        application_client=recovery_client,
    )
    recovery_context = _claim_context(database_engine, operation_id, "recovery-owner")
    recovered = asyncio.run(recovery_handler.execute(recovery_context))
    assert recovered.outcome is OperationOutcome.SUCCEEDED
    assert recovery_client.actions == ["created"]

    with factory() as session:
        release_count = session.scalar(
            select(func.count()).select_from(Release).where(Release.build_id == build_id)
        )
        deployment_count = session.scalar(
            select(func.count())
            .select_from(Deployment)
            .where(Deployment.operation_id == operation_id)
        )
        assert release_count == 1
        assert deployment_count == 1
