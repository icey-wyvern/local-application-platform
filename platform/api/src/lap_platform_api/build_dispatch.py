from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import httpx
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from lap_platform_api.build_agent import (
    BuildAgentError,
    BuildRequest,
    BuildResult,
    Revision,
    RevisionType,
)
from lap_platform_api.kubernetes import (
    ApplicationApplyResult,
    ApplicationResource,
    DesiredApplication,
    KubernetesApplicationRejectedError,
    KubernetesApplicationRolloutError,
    KubernetesApplicationTransientError,
)
from lap_platform_api.metrics import ApiMetrics
from lap_platform_api.repositories import (
    BuildDispatchRecord,
    EntityStateError,
    OperationLeaseLostError,
    ReleaseDeploymentIntent,
    ReleasePersistenceResult,
    RollbackState,
    assert_manual_rollback_apply_allowed,
    assert_release_apply_allowed,
    assert_rollback_apply_allowed,
    get_failed_release_deployment,
    prepare_automatic_rollback,
    prepare_build_dispatch,
    prepare_manual_rollback,
    prepare_release_deployment,
    record_deployment_observed,
    record_deployment_verifying,
    record_healthy_deployment,
    record_retryable_build_failure,
    record_successful_build,
    record_terminal_build_failure,
    record_terminal_deployment_failure,
)
from lap_platform_api.scheduler import (
    OperationExecutionContext,
    OperationResult,
)
from lap_platform_api.states import BuildStatus

LOGGER = logging.getLogger(__name__)
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
ERROR_CODE_PATTERN = re.compile(r"[^A-Z0-9]+")
VULNERABILITY_REJECTION = "VULNERABILITY_POLICY_REJECTED"


class BuildAgent(Protocol):
    async def submit_build(self, request: BuildRequest) -> BuildResult: ...


class ApplicationControlPlane(Protocol):
    async def get(self, name: str) -> ApplicationResource | None: ...

    async def apply(
        self,
        desired: DesiredApplication,
        current: ApplicationResource | None,
    ) -> ApplicationApplyResult: ...

    async def wait_until_observed(
        self,
        desired: DesiredApplication,
        generation: int,
        lease_lost: asyncio.Event,
    ) -> ApplicationResource: ...

    async def wait_until_ready(
        self,
        desired: DesiredApplication,
        generation: int,
        lease_lost: asyncio.Event,
        timeout_seconds: float,
    ) -> ApplicationResource: ...

    async def verify_external_health(
        self,
        desired: DesiredApplication,
        lease_lost: asyncio.Event,
        timeout_seconds: float,
    ) -> None: ...


def _machine_code(value: str) -> str:
    normalized = ERROR_CODE_PATTERN.sub("_", value.upper()).strip("_")
    return normalized[:100] or "BUILD_AGENT_ERROR"


def _revision(value: str) -> Revision:
    if value.startswith("refs/heads/"):
        return Revision(type=RevisionType.BRANCH, value=value.removeprefix("refs/heads/"))
    if value.startswith("refs/tags/"):
        return Revision(type=RevisionType.TAG, value=value.removeprefix("refs/tags/"))
    if COMMIT_PATTERN.fullmatch(value):
        return Revision(type=RevisionType.COMMIT, value=value.lower())
    raise ValueError("stored build revision is invalid")


class DeployBuildHandler:
    """Dispatch a durable deploy Build and atomically persist its immutable Release."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        build_agent: BuildAgent,
        metrics: ApiMetrics,
        *,
        application_client: ApplicationControlPlane,
        deployment_timeout_seconds: float = 300.0,
        tracer: Tracer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._build_agent = build_agent
        self._application_client = application_client
        self._metrics = metrics
        self._deployment_timeout_seconds = deployment_timeout_seconds
        self._tracer = tracer or trace.get_tracer("lap-platform-api.build-dispatch")

    async def execute(self, context: OperationExecutionContext) -> OperationResult:
        try:
            build = await asyncio.to_thread(self._prepare, context)
        except OperationLeaseLostError:
            context.lease_lost.set()
            return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
        except EntityStateError as error:
            return OperationResult.failed("BUILD_STATE_INVALID", str(error))

        if build.status == BuildStatus.SUCCEEDED.value and build.release_id is not None:
            self._metrics.release_creations.labels("reused").inc()
            resumed = await self._resume_failed_deployment(context)
            if resumed is not None:
                return resumed
            return await self._apply_release(context)
        if build.status in {BuildStatus.FAILED.value, BuildStatus.REJECTED.value}:
            return OperationResult.failed(
                build.failure_code or "BUILD_FAILED",
                build.failure_message or "build previously reached a terminal failure",
                {"retryable": False},
            )

        try:
            revision = _revision(build.requested_revision)
        except (ValueError, ValidationError) as error:
            return await self._terminal(
                context,
                "REVISION_INVALID",
                str(error),
                rejected=False,
                stage="clone",
            )

        request = BuildRequest(
            buildId=build.id,
            repositoryUrl=build.repository_url,
            revision=revision,
        )
        started = time.monotonic()
        LOGGER.info(
            "dispatching build to build agent",
            extra={
                "operation_id": str(context.operation_id),
                "build_id": str(build.id),
                "attempt": context.attempt,
            },
        )
        with self._tracer.start_as_current_span(
            "build.dispatch",
            attributes={
                "lap.build.id": str(build.id),
                "lap.operation.attempt": context.attempt,
            },
        ) as span:
            try:
                result = await self._build_agent.submit_build(request)
                if result.buildId != build.id:
                    raise ValueError("build agent returned a different build identifier")
            except BuildAgentError as error:
                return await self._agent_failure(context, error, span, started)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                return await self._retryable(
                    context,
                    "BUILD_AGENT_UNAVAILABLE",
                    "build agent request failed",
                    stage=None,
                    span=span,
                    started=started,
                    exception=error,
                )
            except (ValueError, ValidationError) as error:
                return await self._terminal(
                    context,
                    "BUILD_AGENT_RESULT_INVALID",
                    str(error),
                    rejected=False,
                    stage=None,
                    span=span,
                    started=started,
                )

            try:
                persisted = await asyncio.to_thread(self._success, context, result)
            except OperationLeaseLostError:
                context.lease_lost.set()
                span.set_status(Status(StatusCode.ERROR, "operation lease lost"))
                return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
            except EntityStateError as error:
                return await self._terminal(
                    context,
                    "APPLICATION_IDENTITY_CONFLICT",
                    str(error),
                    rejected=False,
                    stage="persist",
                    span=span,
                    started=started,
                )
            except IntegrityError:
                return await self._terminal(
                    context,
                    "APPLICATION_IDENTITY_CONFLICT",
                    "application name or repository conflicts with durable identity history",
                    rejected=False,
                    stage="persist",
                    span=span,
                    started=started,
                )

            duration = time.monotonic() - started
            self._metrics.build_dispatches.labels("succeeded").inc()
            self._metrics.build_agent_duration.labels("succeeded").observe(duration)
            self._metrics.build_results.labels("succeeded", "none").inc()
            self._metrics.release_creations.labels(
                "created" if persisted.created else "reused"
            ).inc()
            span.set_attribute("lap.build.result", "succeeded")
            LOGGER.info(
                "build and release persisted",
                extra={
                    "operation_id": str(context.operation_id),
                    "build_id": str(build.id),
                    "application_name": result.application.name,
                    "release_id": str(persisted.release.id),
                    "duration_ms": round(duration * 1000, 3),
                    "outcome": "succeeded",
                },
            )
            return await self._apply_release(context)

    async def _apply_release(self, context: OperationExecutionContext) -> OperationResult:
        started = time.monotonic()
        with self._tracer.start_as_current_span("application.apply") as span:
            try:
                intent = await asyncio.to_thread(self._prepare_deployment, context)
                desired = DesiredApplication(
                    name=intent.application_name,
                    release_id=intent.release_id,
                    image=intent.image_reference,
                    port=intent.port,
                    health_path=intent.health_path,
                )
                current = await self._application_client.get(desired.name)
                current_release_id = current.spec.release_id if current is not None else None
                await asyncio.to_thread(
                    self._assert_apply_allowed,
                    context,
                    current_release_id,
                )
                applied = await self._application_client.apply(desired, current)
                self._record_application_action(applied)
                await asyncio.to_thread(self._record_observed, context)
                remaining = self._remaining_deployment_seconds(intent)
                observed = await self._application_client.wait_until_ready(
                    desired,
                    applied.resource.metadata.generation,
                    context.lease_lost,
                    remaining,
                )
                if observed.spec.release_id != intent.release_id:
                    raise KubernetesApplicationTransientError(
                        "Application desired release changed during observation"
                    )
                await asyncio.to_thread(self._record_verifying, context)
                self._metrics.deployment_acceptance_stages.labels(
                    "controller_ready", "succeeded"
                ).inc()
                await self._application_client.verify_external_health(
                    desired,
                    context.lease_lost,
                    self._remaining_deployment_seconds(intent),
                )
                self._metrics.deployment_acceptance_stages.labels(
                    "external_https", "succeeded"
                ).inc()
                await asyncio.to_thread(self._record_healthy, context)
            except OperationLeaseLostError:
                context.lease_lost.set()
                span.set_status(Status(StatusCode.ERROR, "operation lease lost"))
                return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
            except KubernetesApplicationTransientError as error:
                self._metrics.application_cr_actions.labels("retryable_failure").inc()
                span.set_status(Status(StatusCode.ERROR, "Kubernetes Application retry"))
                LOGGER.warning(
                    "Application desired state will be retried",
                    extra={
                        "operation_id": str(context.operation_id),
                        "error_code": "KUBERNETES_APPLICATION_UNAVAILABLE",
                        "retryable": True,
                    },
                )
                return OperationResult.retry(
                    "KUBERNETES_APPLICATION_UNAVAILABLE",
                    str(error),
                    {"retryable": True, "stage": "application_apply"},
                )
            except KubernetesApplicationRolloutError as error:
                self._metrics.deployment_acceptance_duration.labels("failed").observe(
                    time.monotonic() - started
                )
                self._metrics.deployment_acceptance_stages.labels(error.stage, "failed").inc()
                return await self._deployment_terminal(
                    context,
                    error.code,
                    str(error),
                    span,
                    stage=error.stage,
                    rollback=True,
                )
            except KubernetesApplicationRejectedError as error:
                return await self._deployment_terminal(
                    context,
                    "APPLICATION_DESIRED_STATE_REJECTED",
                    str(error),
                    span,
                    stage="application_apply",
                )
            except EntityStateError as error:
                return await self._deployment_terminal(
                    context,
                    "APPLICATION_DEPLOYMENT_CONFLICT",
                    str(error),
                    span,
                    stage="application_apply",
                )
            except IntegrityError:
                return await self._deployment_terminal(
                    context,
                    "APPLICATION_DEPLOYMENT_CONFLICT",
                    "another active Deployment already owns this Application",
                    span,
                    stage="application_apply",
                )

            duration = time.monotonic() - started
            self._metrics.application_observation_duration.labels("accepted").observe(duration)
            self._metrics.deployment_acceptance_duration.labels("succeeded").observe(duration)
            span.set_attribute("lap.application.apply_result", applied.action)
            LOGGER.info(
                "Application deployment accepted as healthy",
                extra={
                    "operation_id": str(context.operation_id),
                    "application_name": intent.application_name,
                    "release_id": str(intent.release_id),
                    "deployment_id": str(intent.deployment_id),
                    "application_generation": applied.resource.metadata.generation,
                    "duration_ms": round(duration * 1000, 3),
                },
            )
            return OperationResult.succeeded()

    def _record_application_action(self, result: ApplicationApplyResult) -> None:
        self._metrics.application_cr_actions.labels(result.action).inc()

    async def _deployment_terminal(
        self,
        context: OperationExecutionContext,
        code: str,
        message: str,
        span: trace.Span,
        *,
        stage: str,
        rollback: bool = False,
    ) -> OperationResult:
        try:
            await asyncio.to_thread(
                self._record_deployment_failure,
                context,
                code,
                message,
            )
        except OperationLeaseLostError:
            context.lease_lost.set()
            return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
        self._metrics.application_cr_actions.labels("terminal_failure").inc()
        span.set_status(Status(StatusCode.ERROR, code))
        if rollback:
            resumed = await self._resume_failed_deployment(context)
            if resumed is not None:
                return resumed
        return OperationResult.failed(
            code,
            message,
            {"retryable": False, "stage": stage},
        )

    async def _resume_failed_deployment(
        self, context: OperationExecutionContext
    ) -> OperationResult | None:
        try:
            failed = await asyncio.to_thread(self._failed_deployment, context)
            if failed is None:
                return None
            failed_id, application_name = failed
            current = await self._application_client.get(application_name)
            state = await asyncio.to_thread(
                self._prepare_rollback,
                context,
                failed_id,
                current.spec.release_id if current is not None else None,
            )
        except OperationLeaseLostError:
            context.lease_lost.set()
            return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
        except KubernetesApplicationTransientError as error:
            return OperationResult.retry(
                "KUBERNETES_APPLICATION_UNAVAILABLE", str(error), {"retryable": True}
            )
        if state.unavailable:
            self._metrics.rollbacks.labels("unavailable").inc()
            LOGGER.warning(
                "automatic rollback unavailable",
                extra={
                    "operation_id": str(context.operation_id),
                    "deployment_id": str(state.failed_deployment_id),
                    "release_id": str(state.failed_release_id),
                },
            )
            return self._rollback_result(state, "unavailable")
        return await self._execute_rollback(context, state)

    async def _execute_rollback(
        self, context: OperationExecutionContext, state: RollbackState
    ) -> OperationResult:
        intent = state.rollback
        assert intent is not None
        if intent.status == "failed":
            return self._rollback_result(state, "failed")
        if intent.status == "healthy":
            return self._rollback_result(state, "succeeded")
        started = time.monotonic()
        self._metrics.rollbacks.labels("started").inc()
        LOGGER.info(
            "automatic rollback started",
            extra={
                "operation_id": str(context.operation_id),
                "deployment_id": str(intent.deployment_id),
                "failed_deployment_id": str(state.failed_deployment_id),
                "release_id": str(intent.release_id),
            },
        )
        desired = DesiredApplication(
            intent.application_name,
            intent.release_id,
            intent.image_reference,
            intent.port,
            intent.health_path,
        )
        try:
            if self._remaining_deployment_seconds(intent) <= 0:
                raise KubernetesApplicationRolloutError(
                    "ROLLBACK_TIMEOUT", "rollback acceptance deadline expired", "runtime"
                )
            current = await self._application_client.get(desired.name)
            await asyncio.to_thread(
                self._assert_rollback_allowed,
                context,
                current.spec.release_id if current is not None else None,
            )
            if current is None:
                raise KubernetesApplicationRolloutError(
                    "APPLICATION_RESOURCE_MISSING",
                    "Application disappeared during rollback",
                    "controller",
                )
            applied = await self._application_client.apply(desired, current)
            self._metrics.application_cr_actions.labels(applied.action).inc()
            await asyncio.to_thread(self._record_observed, context)
            await self._application_client.wait_until_ready(
                desired,
                applied.resource.metadata.generation,
                context.lease_lost,
                self._remaining_deployment_seconds(intent),
            )
            await asyncio.to_thread(self._record_verifying, context)
            self._metrics.rollback_acceptance_stages.labels("controller_ready", "succeeded").inc()
            await self._application_client.verify_external_health(
                desired, context.lease_lost, self._remaining_deployment_seconds(intent)
            )
            self._metrics.rollback_acceptance_stages.labels("external_https", "succeeded").inc()
            await asyncio.to_thread(self._record_healthy, context)
        except OperationLeaseLostError:
            context.lease_lost.set()
            return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
        except KubernetesApplicationTransientError as error:
            if context.lease_lost.is_set():
                return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
            if self._remaining_deployment_seconds(intent) <= 0:
                return await self._fail_rollback(
                    context,
                    state,
                    "ROLLBACK_TIMEOUT",
                    "rollback acceptance deadline expired",
                    "runtime",
                    started,
                )
            return OperationResult.retry(
                "KUBERNETES_APPLICATION_UNAVAILABLE", str(error), {"retryable": True}
            )
        except (
            KubernetesApplicationRolloutError,
            KubernetesApplicationRejectedError,
            EntityStateError,
        ) as error:
            code = (
                error.code
                if isinstance(error, KubernetesApplicationRolloutError)
                else "ROLLBACK_FAILED"
            )
            stage = (
                error.stage
                if isinstance(error, KubernetesApplicationRolloutError)
                else "application_apply"
            )
            return await self._fail_rollback(context, state, code, str(error), stage, started)
        self._metrics.rollbacks.labels("succeeded").inc()
        self._metrics.rollback_duration.labels("succeeded").observe(time.monotonic() - started)
        LOGGER.info(
            "automatic rollback accepted as healthy",
            extra={
                "operation_id": str(context.operation_id),
                "deployment_id": str(intent.deployment_id),
                "release_id": str(intent.release_id),
            },
        )
        return self._rollback_result(state, "succeeded")

    async def _fail_rollback(
        self,
        context: OperationExecutionContext,
        state: RollbackState,
        code: str,
        message: str,
        stage: str,
        started: float,
    ) -> OperationResult:
        intent = state.rollback
        assert intent is not None
        try:
            await asyncio.to_thread(self._record_deployment_failure, context, code, message)
        except OperationLeaseLostError:
            context.lease_lost.set()
            return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
        self._metrics.rollbacks.labels("failed").inc()
        self._metrics.rollback_acceptance_stages.labels(stage, "failed").inc()
        self._metrics.rollback_duration.labels("failed").observe(time.monotonic() - started)
        LOGGER.error(
            "automatic rollback failed",
            extra={
                "operation_id": str(context.operation_id),
                "deployment_id": str(intent.deployment_id),
                "error_code": code,
            },
        )
        return self._rollback_result(state, "failed", code, message)

    @staticmethod
    def _rollback_result(
        state: RollbackState,
        outcome: str,
        rollback_code: str | None = None,
        rollback_message: str | None = None,
    ) -> OperationResult:
        intent = state.rollback
        result = {
            "retryable": False,
            "rollbackStatus": outcome,
            "failedDeploymentId": str(state.failed_deployment_id),
            "failedReleaseId": str(state.failed_release_id),
            "rollbackDeploymentId": str(intent.deployment_id) if intent else None,
            "rollbackTargetReleaseId": str(intent.release_id) if intent else None,
            "restoredReleaseId": str(intent.release_id)
            if intent and outcome == "succeeded"
            else None,
            "rollbackFailureCode": rollback_code or state.rollback_failure_code,
            "rollbackFailureMessage": rollback_message or state.rollback_failure_message,
        }
        code = (
            "ROLLBACK_TARGET_MISSING"
            if outcome == "unavailable"
            else "ROLLBACK_FAILED"
            if outcome == "failed"
            else state.failure_code
        )
        return OperationResult.failed(code, state.failure_message, result)

    def _failed_deployment(self, context: OperationExecutionContext) -> tuple[UUID, str] | None:
        with self._session_factory.begin() as session:
            failed = get_failed_release_deployment(
                session, operation_id=context.operation_id, lease_owner=context.lease_owner
            )
            return (failed.id, failed.application.name) if failed is not None else None

    def _prepare_rollback(
        self,
        context: OperationExecutionContext,
        failed_id: UUID,
        current_release_id: UUID | None,
    ) -> RollbackState:
        with self._session_factory.begin() as session:
            return prepare_automatic_rollback(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
                failed_deployment_id=failed_id,
                current_cr_release_id=current_release_id,
            )

    def _assert_rollback_allowed(
        self, context: OperationExecutionContext, current_release_id: UUID | None
    ) -> None:
        with self._session_factory.begin() as session:
            assert_rollback_apply_allowed(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
                current_release_id=current_release_id,
            )

    def _remaining_deployment_seconds(self, intent: ReleaseDeploymentIntent) -> float:
        elapsed = (datetime.now(UTC) - intent.started_at).total_seconds()
        return max(0.0, self._deployment_timeout_seconds - elapsed)

    async def _agent_failure(
        self,
        context: OperationExecutionContext,
        error: BuildAgentError,
        span: trace.Span,
        started: float,
    ) -> OperationResult:
        code = _machine_code(error.detail.code)
        if error.detail.retryable:
            return await self._retryable(
                context,
                code,
                error.detail.message,
                stage=error.detail.stage,
                span=span,
                started=started,
                status_code=error.status_code,
            )
        return await self._terminal(
            context,
            code,
            error.detail.message,
            rejected=code == VULNERABILITY_REJECTION,
            stage=error.detail.stage,
            span=span,
            started=started,
            status_code=error.status_code,
        )

    async def _retryable(
        self,
        context: OperationExecutionContext,
        code: str,
        message: str,
        *,
        stage: str | None,
        span: trace.Span,
        started: float,
        status_code: int | None = None,
        exception: Exception | None = None,
    ) -> OperationResult:
        try:
            await asyncio.to_thread(self._record_retry, context, code, message)
        except OperationLeaseLostError:
            context.lease_lost.set()
            return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
        duration = time.monotonic() - started
        self._metrics.build_dispatches.labels("retryable_failure").inc()
        self._metrics.build_agent_duration.labels("retryable_failure").observe(duration)
        self._metrics.build_results.labels("retry", "infrastructure").inc()
        span.set_status(Status(StatusCode.ERROR, code))
        LOGGER.warning(
            "build dispatch will be retried",
            exc_info=exception,
            extra={
                "operation_id": str(context.operation_id),
                "error_code": code,
                "build_stage": stage,
                "retryable": True,
                "duration_ms": round(duration * 1000, 3),
            },
        )
        return OperationResult.retry(
            code,
            message,
            {
                "retryable": True,
                "stage": stage,
                "buildAgentStatus": status_code,
            },
        )

    async def _terminal(
        self,
        context: OperationExecutionContext,
        code: str,
        message: str,
        *,
        rejected: bool,
        stage: str | None,
        span: trace.Span | None = None,
        started: float | None = None,
        status_code: int | None = None,
    ) -> OperationResult:
        try:
            await asyncio.to_thread(self._record_terminal, context, code, message, rejected)
        except OperationLeaseLostError:
            context.lease_lost.set()
            return OperationResult.retry("OPERATION_LEASE_LOST", "operation lease was lost")
        duration = time.monotonic() - started if started is not None else 0
        self._metrics.build_dispatches.labels("terminal_failure").inc()
        if started is not None:
            self._metrics.build_agent_duration.labels("terminal_failure").observe(duration)
        failure_kind = "policy" if rejected else "request_or_build"
        self._metrics.build_results.labels("failed", failure_kind).inc()
        if span is not None:
            span.set_status(Status(StatusCode.ERROR, code))
        LOGGER.warning(
            "build failed terminally",
            extra={
                "operation_id": str(context.operation_id),
                "error_code": code,
                "build_stage": stage,
                "retryable": False,
                "duration_ms": round(duration * 1000, 3),
            },
        )
        return OperationResult.failed(
            code,
            message,
            {
                "retryable": False,
                "stage": stage,
                "buildAgentStatus": status_code,
            },
        )

    def _prepare(self, context: OperationExecutionContext) -> BuildDispatchRecord:
        with self._session_factory.begin() as session:
            return prepare_build_dispatch(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
            )

    def _record_retry(
        self,
        context: OperationExecutionContext,
        code: str,
        message: str,
    ) -> None:
        with self._session_factory.begin() as session:
            record_retryable_build_failure(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
                failure_code=code,
                failure_message=message,
            )

    def _record_terminal(
        self,
        context: OperationExecutionContext,
        code: str,
        message: str,
        rejected: bool,
    ) -> None:
        with self._session_factory.begin() as session:
            record_terminal_build_failure(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
                failure_code=code,
                failure_message=message,
                rejected=rejected,
            )

    def _success(
        self,
        context: OperationExecutionContext,
        result: BuildResult,
    ) -> ReleasePersistenceResult:
        with self._session_factory.begin() as session:
            return record_successful_build(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
                application_name=result.application.name,
                resolved_commit=result.commit,
                image_reference=result.imageReference,
                image_digest=result.imageDigest,
                port=result.application.port,
                health_path=result.application.healthPath,
                vulnerability_counts=result.vulnerabilities.counts,
            )

    def _prepare_deployment(
        self,
        context: OperationExecutionContext,
    ) -> ReleaseDeploymentIntent:
        with self._session_factory.begin() as session:
            return prepare_release_deployment(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
            )

    def _assert_apply_allowed(
        self,
        context: OperationExecutionContext,
        current_release_id: UUID | None,
    ) -> None:
        with self._session_factory.begin() as session:
            assert_release_apply_allowed(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
                current_release_id=current_release_id,
            )

    def _record_observed(self, context: OperationExecutionContext) -> None:
        with self._session_factory.begin() as session:
            record_deployment_observed(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
            )

    def _record_verifying(self, context: OperationExecutionContext) -> None:
        with self._session_factory.begin() as session:
            record_deployment_verifying(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
            )

    def _record_healthy(self, context: OperationExecutionContext) -> None:
        with self._session_factory.begin() as session:
            record_healthy_deployment(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
            )

    def _record_deployment_failure(
        self,
        context: OperationExecutionContext,
        code: str,
        message: str,
    ) -> None:
        with self._session_factory.begin() as session:
            record_terminal_deployment_failure(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
                failure_code=code,
                failure_message=message,
            )


class ManualRollbackHandler(DeployBuildHandler):
    """Apply an already persisted Release through the normal rollout acceptance path."""

    async def execute(self, context: OperationExecutionContext) -> OperationResult:
        return await self._apply_release(context)

    def _prepare_deployment(self, context: OperationExecutionContext) -> ReleaseDeploymentIntent:
        with self._session_factory.begin() as session:
            return prepare_manual_rollback(
                session, operation_id=context.operation_id, lease_owner=context.lease_owner
            )

    def _assert_apply_allowed(
        self, context: OperationExecutionContext, current_release_id: UUID | None
    ) -> None:
        with self._session_factory.begin() as session:
            assert_manual_rollback_apply_allowed(
                session,
                operation_id=context.operation_id,
                lease_owner=context.lease_owner,
                current_release_id=current_release_id,
            )
