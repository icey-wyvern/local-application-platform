from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID, uuid4

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer
from sqlalchemy.orm import Session, sessionmaker

from lap_platform_api.metrics import ApiMetrics
from lap_platform_api.repositories import (
    OPERATION_LEASE_DURATION,
    OPERATION_LEASE_RENEWAL_INTERVAL,
    OperationClaim,
    claim_operation_work,
    complete_operation,
    count_queued_operations,
    fail_operation,
    release_operation_lease,
    renew_operation_lease,
    requeue_operation,
)
from lap_platform_api.states import OperationType

LOGGER = logging.getLogger(__name__)
MAX_CONCURRENT_OPERATIONS = 2


class OperationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRY = "retry"
    FAILED = "failed"


@dataclass(frozen=True)
class OperationResult:
    outcome: OperationOutcome
    failure_code: str | None = None
    failure_message: str | None = None
    failure_result: dict[str, Any] | None = None

    @classmethod
    def succeeded(cls) -> OperationResult:
        return cls(OperationOutcome.SUCCEEDED)

    @classmethod
    def retry(
        cls,
        code: str,
        message: str,
        result: dict[str, Any] | None = None,
    ) -> OperationResult:
        return cls(OperationOutcome.RETRY, code, message, result)

    @classmethod
    def failed(
        cls,
        code: str,
        message: str,
        result: dict[str, Any] | None = None,
    ) -> OperationResult:
        return cls(OperationOutcome.FAILED, code, message, result)


class RetryableOperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


class TerminalOperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


@dataclass(frozen=True)
class OperationExecutionContext:
    operation_id: UUID
    operation_type: str
    correlation_id: UUID
    attempt: int
    lease_owner: str
    lease_lost: asyncio.Event = field(compare=False)


class OperationHandler(Protocol):
    async def execute(self, context: OperationExecutionContext) -> OperationResult: ...


class NoOpOperationHandler:
    """Temporary handler used until operation-specific execution is implemented."""

    def __init__(self, delay_seconds: float = 0) -> None:
        self._delay_seconds = delay_seconds

    async def execute(self, context: OperationExecutionContext) -> OperationResult:
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        return OperationResult.succeeded()


def noop_operation_handlers(delay_seconds: float = 0) -> Mapping[str, OperationHandler]:
    handler = NoOpOperationHandler(delay_seconds)
    return MappingProxyType({operation_type.value: handler for operation_type in OperationType})


class OperationScheduler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: Mapping[str, OperationHandler],
        metrics: ApiMetrics,
        *,
        lease_owner: str | None = None,
        concurrency: int = MAX_CONCURRENT_OPERATIONS,
        poll_interval_seconds: float = 1,
        retry_delay_seconds: float = 1,
        lease_duration: timedelta = OPERATION_LEASE_DURATION,
        renewal_interval: timedelta = OPERATION_LEASE_RENEWAL_INTERVAL,
        tracer: Tracer | None = None,
    ) -> None:
        if not 1 <= concurrency <= MAX_CONCURRENT_OPERATIONS:
            raise ValueError("scheduler concurrency must be between one and two")
        if poll_interval_seconds <= 0:
            raise ValueError("scheduler poll interval must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("scheduler retry delay must not be negative")
        if renewal_interval <= timedelta(0) or renewal_interval >= lease_duration:
            raise ValueError("lease renewal interval must be positive and shorter than the lease")
        self._session_factory = session_factory
        self._handlers = handlers
        self._metrics = metrics
        self._lease_owner = lease_owner or self._new_lease_owner()
        self._concurrency = concurrency
        self._poll_interval_seconds = poll_interval_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._lease_duration = lease_duration
        self._renewal_interval_seconds = renewal_interval.total_seconds()
        self._tracer = tracer or trace.get_tracer("lap-platform-api.scheduler")
        self._stop = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._active: set[asyncio.Task[None]] = set()

    @staticmethod
    def _new_lease_owner() -> str:
        pod = os.environ.get("POD_NAME", socket.gethostname())
        return f"{pod}:{os.getpid()}:{uuid4()}"

    @property
    def lease_owner(self) -> str:
        return self._lease_owner

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._stop.clear()
        self._runner = asyncio.create_task(
            self._run(), name=f"operation-scheduler:{self._lease_owner}"
        )
        LOGGER.info("operation scheduler started", extra={"lease_owner": self._lease_owner})

    async def stop(self) -> None:
        if self._runner is None:
            return
        self._stop.set()
        await self._runner
        self._runner = None
        LOGGER.info("operation scheduler stopped", extra={"lease_owner": self._lease_owner})

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                available = self._concurrency - len(self._active)
                if available > 0:
                    try:
                        claims, queue_depth = await asyncio.to_thread(self._claim, available)
                        self._metrics.scheduler_queue_depth.set(queue_depth)
                        for claim in claims:
                            claim_kind = "reclaimed" if claim.reclaimed else "new"
                            self._metrics.scheduler_claims.labels(claim.type, claim_kind).inc()
                            LOGGER.info(
                                "operation claimed",
                                extra={
                                    "operation_id": str(claim.id),
                                    "operation_type": claim.type,
                                    "attempt": claim.attempt,
                                    "lease_owner": self._lease_owner,
                                    "claim_kind": claim_kind,
                                },
                            )
                            task = asyncio.create_task(
                                self._execute_claim(claim),
                                name=f"operation:{claim.id}",
                            )
                            self._active.add(task)
                            task.add_done_callback(self._active.discard)
                    except Exception:
                        self._metrics.scheduler_errors.labels("claim").inc()
                        LOGGER.exception("operation scheduler claim failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval_seconds)
                except TimeoutError:
                    pass
        finally:
            active = tuple(self._active)
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)

    def _claim(self, limit: int) -> tuple[list[OperationClaim], int]:
        with self._session_factory.begin() as session:
            claims = claim_operation_work(
                session,
                lease_owner=self._lease_owner,
                limit=limit,
                lease_duration=self._lease_duration,
            )
            queue_depth = count_queued_operations(session)
        return claims, queue_depth

    async def _execute_claim(self, claim: OperationClaim) -> None:
        started = time.monotonic()
        lease_lost = asyncio.Event()
        context = OperationExecutionContext(
            operation_id=claim.id,
            operation_type=claim.type,
            correlation_id=claim.correlation_id,
            attempt=claim.attempt,
            lease_owner=self._lease_owner,
            lease_lost=lease_lost,
        )
        handler = self._handlers.get(claim.type)
        self._metrics.scheduler_running.labels(claim.type).inc()
        outcome = "lease_lost"
        with self._tracer.start_as_current_span(
            "operation.execute",
            attributes={
                "lap.operation.id": str(claim.id),
                "lap.operation.type": claim.type,
                "lap.operation.attempt": claim.attempt,
                "lap.operation.correlation_id": str(claim.correlation_id),
                "lap.operation.reclaimed": claim.reclaimed,
            },
        ) as span:
            execution = asyncio.create_task(
                self._invoke_handler(handler, context),
                name=f"operation-handler:{claim.id}",
            )
            renewal = asyncio.create_task(
                self._renew_lease(claim, execution, lease_lost),
                name=f"operation-renewal:{claim.id}",
            )
            try:
                result = await execution
                if lease_lost.is_set():
                    span.set_status(Status(StatusCode.ERROR, "operation lease lost"))
                    return
                if result.outcome == OperationOutcome.RETRY and self._retry_delay_seconds:
                    await asyncio.sleep(self._retry_delay_seconds)
                persisted = await asyncio.to_thread(self._persist_result, claim, result)
                if not persisted:
                    lease_lost.set()
                    self._record_lease_loss(claim)
                    span.set_status(Status(StatusCode.ERROR, "operation lease lost"))
                    return
                outcome = result.outcome.value
                span.set_attribute("lap.operation.outcome", outcome)
                if result.outcome != OperationOutcome.SUCCEEDED:
                    span.set_status(Status(StatusCode.ERROR, result.failure_code or outcome))
                LOGGER.info(
                    "operation execution finished",
                    extra={
                        "operation_id": str(claim.id),
                        "operation_type": claim.type,
                        "attempt": claim.attempt,
                        "lease_owner": self._lease_owner,
                        "outcome": outcome,
                        "error_code": result.failure_code,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    },
                )
            except asyncio.CancelledError:
                if lease_lost.is_set():
                    span.set_status(Status(StatusCode.ERROR, "operation lease lost"))
                    return
                released = await asyncio.shield(asyncio.to_thread(self._release, claim.id))
                outcome = "shutdown_requeued" if released else "shutdown_lease_lost"
                span.set_attribute("lap.operation.outcome", outcome)
                LOGGER.info(
                    "operation interrupted during scheduler shutdown",
                    extra={
                        "operation_id": str(claim.id),
                        "operation_type": claim.type,
                        "attempt": claim.attempt,
                        "lease_owner": self._lease_owner,
                        "outcome": outcome,
                    },
                )
                return
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
                duration = time.monotonic() - started
                self._metrics.scheduler_running.labels(claim.type).dec()
                self._metrics.scheduler_outcomes.labels(claim.type, outcome).inc()
                self._metrics.scheduler_execution_duration.labels(claim.type, outcome).observe(
                    duration
                )

    async def _invoke_handler(
        self,
        handler: OperationHandler | None,
        context: OperationExecutionContext,
    ) -> OperationResult:
        if handler is None:
            return OperationResult.failed(
                "OPERATION_TYPE_UNSUPPORTED",
                f"no handler is registered for operation type {context.operation_type}",
            )
        try:
            return await handler.execute(context)
        except RetryableOperationError as error:
            return OperationResult.retry(error.code, str(error), error.result)
        except TerminalOperationError as error:
            return OperationResult.failed(error.code, str(error), error.result)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "operation handler raised an unexpected error",
                extra={
                    "operation_id": str(context.operation_id),
                    "operation_type": context.operation_type,
                    "attempt": context.attempt,
                    "lease_owner": self._lease_owner,
                },
            )
            return OperationResult.retry(
                "OPERATION_HANDLER_ERROR",
                "operation handler raised an unexpected error",
                {"retryable": True},
            )

    async def _renew_lease(
        self,
        claim: OperationClaim,
        execution: asyncio.Task[OperationResult],
        lease_lost: asyncio.Event,
    ) -> None:
        while True:
            await asyncio.sleep(self._renewal_interval_seconds)
            renewed = await asyncio.to_thread(self._renew, claim.id)
            if renewed:
                LOGGER.debug(
                    "operation lease renewed",
                    extra={
                        "operation_id": str(claim.id),
                        "operation_type": claim.type,
                        "attempt": claim.attempt,
                        "lease_owner": self._lease_owner,
                    },
                )
                continue
            lease_lost.set()
            self._record_lease_loss(claim)
            execution.cancel()
            return

    def _record_lease_loss(self, claim: OperationClaim) -> None:
        self._metrics.scheduler_lease_renewal_failures.labels(claim.type).inc()
        LOGGER.warning(
            "operation lease lost",
            extra={
                "operation_id": str(claim.id),
                "operation_type": claim.type,
                "attempt": claim.attempt,
                "lease_owner": self._lease_owner,
            },
        )

    def _renew(self, operation_id: UUID) -> bool:
        with self._session_factory.begin() as session:
            return renew_operation_lease(
                session,
                operation_id=operation_id,
                lease_owner=self._lease_owner,
                lease_duration=self._lease_duration,
            )

    def _release(self, operation_id: UUID) -> bool:
        try:
            with self._session_factory.begin() as session:
                return release_operation_lease(
                    session,
                    operation_id=operation_id,
                    lease_owner=self._lease_owner,
                )
        except Exception:
            self._metrics.scheduler_errors.labels("shutdown_release").inc()
            LOGGER.exception(
                "operation lease release failed during shutdown",
                extra={
                    "operation_id": str(operation_id),
                    "lease_owner": self._lease_owner,
                },
            )
            return False

    def _persist_result(self, claim: OperationClaim, result: OperationResult) -> bool:
        with self._session_factory.begin() as session:
            if result.outcome == OperationOutcome.SUCCEEDED:
                return complete_operation(
                    session,
                    operation_id=claim.id,
                    lease_owner=self._lease_owner,
                )
            failure_code = result.failure_code or "OPERATION_FAILED"
            failure_message = result.failure_message or "operation failed"
            if result.outcome == OperationOutcome.RETRY:
                return requeue_operation(
                    session,
                    operation_id=claim.id,
                    lease_owner=self._lease_owner,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    failure_result=result.failure_result,
                )
            return fail_operation(
                session,
                operation_id=claim.id,
                lease_owner=self._lease_owner,
                failure_code=failure_code,
                failure_message=failure_message,
                failure_result=result.failure_result,
            )
