from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from lap_platform_api.database import create_session_factory
from lap_platform_api.metrics import ApiMetrics
from lap_platform_api.models import Operation
from lap_platform_api.repositories import (
    claim_pending_operations,
    create_idempotent_operation,
)
from lap_platform_api.scheduler import (
    OperationExecutionContext,
    OperationHandler,
    OperationResult,
    OperationScheduler,
)
from lap_platform_api.states import OperationStatus, OperationType
from sqlalchemy import Engine, select, update

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
def isolate_scheduler_queue(database_engine: Engine, migrated_database: None) -> None:
    factory = create_session_factory(database_engine)
    with factory.begin() as session:
        session.execute(
            update(Operation)
            .where(
                Operation.status.in_([
                    OperationStatus.QUEUED.value,
                    OperationStatus.RUNNING.value,
                ])
            )
            .values(
                status=OperationStatus.FAILED.value,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=datetime.now(UTC),
                failure_code="TEST_QUEUE_ISOLATION",
                failure_message="disposable operation isolated between scheduler tests",
            )
        )


class BlockingHandler:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls: list[UUID] = []
        self.active = 0
        self.maximum_active = 0
        self.cancelled = 0

    async def execute(self, context: OperationExecutionContext) -> OperationResult:
        self.calls.append(context.operation_id)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1
        return OperationResult.succeeded()


class ImmediateHandler:
    async def execute(self, context: OperationExecutionContext) -> OperationResult:
        return OperationResult.succeeded()


class OutcomeHandler:
    def __init__(self, terminal_id: UUID, retry_id: UUID) -> None:
        self._terminal_id = terminal_id
        self._retry_id = retry_id
        self._calls: Counter[UUID] = Counter()
        self.retry_second_attempt = asyncio.Event()

    async def execute(self, context: OperationExecutionContext) -> OperationResult:
        self._calls[context.operation_id] += 1
        if context.operation_id == self._terminal_id:
            return OperationResult.failed(
                "TEST_TERMINAL_FAILURE",
                "test terminal failure",
                {"retryable": False},
            )
        if context.operation_id == self._retry_id and self._calls[context.operation_id] == 1:
            return OperationResult.retry(
                "TEST_RETRYABLE_FAILURE",
                "test retryable failure",
                {"retryable": True},
            )
        if context.operation_id == self._retry_id:
            await self.retry_second_attempt.wait()
        return OperationResult.succeeded()


def _handlers(handler: OperationHandler) -> dict[str, OperationHandler]:
    return {operation_type.value: handler for operation_type in OperationType}


def _create_operations(engine: Engine, count: int) -> list[UUID]:
    factory = create_session_factory(engine)
    operation_ids: list[UUID] = []
    with factory.begin() as session:
        for _ in range(count):
            repository = f"https://example.com/scheduler-{uuid4()}.git"
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
            operation_ids.append(operation.id)
    return operation_ids


def _load_operations(engine: Engine, operation_ids: list[UUID]) -> list[Operation]:
    factory = create_session_factory(engine)
    with factory() as session:
        return list(
            session.scalars(
                select(Operation).where(Operation.id.in_(operation_ids)).order_by(Operation.id)
            )
        )


async def _wait_until(
    predicate: Callable[[], bool | Coroutine[Any, Any, bool]],
    *,
    timeout: float = 5,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not satisfied before timeout")
        await asyncio.sleep(0.01)


def _scheduler(
    engine: Engine,
    handler: OperationHandler,
    metrics: ApiMetrics,
    owner: str,
    *,
    lease_seconds: float = 1,
    renewal_seconds: float = 0.1,
) -> OperationScheduler:
    return OperationScheduler(
        create_session_factory(engine),
        _handlers(handler),
        metrics,
        lease_owner=owner,
        poll_interval_seconds=0.02,
        retry_delay_seconds=0.01,
        lease_duration=timedelta(seconds=lease_seconds),
        renewal_interval=timedelta(seconds=renewal_seconds),
    )


def test_two_schedulers_claim_distinct_work_and_each_limits_concurrency(
    database_engine: Engine,
) -> None:
    operation_ids = _create_operations(database_engine, 6)

    async def exercise() -> None:
        first_handler = BlockingHandler()
        second_handler = BlockingHandler()
        first = _scheduler(database_engine, first_handler, ApiMetrics(), "scheduler-one")
        second = _scheduler(database_engine, second_handler, ApiMetrics(), "scheduler-two")
        await first.start()
        await second.start()
        try:
            await _wait_until(lambda: first_handler.active == second_handler.active == 2)
            assert first_handler.maximum_active == second_handler.maximum_active == 2
            assert set(first_handler.calls).isdisjoint(second_handler.calls)
            assert len(set(first_handler.calls + second_handler.calls)) == 4

            first_handler.release.set()
            second_handler.release.set()
            await _wait_until(
                lambda: all(
                    operation.status == OperationStatus.SUCCEEDED.value
                    for operation in _load_operations(database_engine, operation_ids)
                )
            )
        finally:
            await first.stop()
            await second.stop()

        calls = Counter(first_handler.calls + second_handler.calls)
        assert calls == Counter({operation_id: 1 for operation_id in operation_ids})

    asyncio.run(exercise())


def test_lease_renewal_and_graceful_shutdown_requeue(database_engine: Engine) -> None:
    operation_id = _create_operations(database_engine, 1)[0]

    async def exercise() -> None:
        handler = BlockingHandler()
        scheduler = _scheduler(
            database_engine,
            handler,
            ApiMetrics(),
            "renewing-scheduler",
            lease_seconds=2,
            renewal_seconds=0.2,
        )
        await scheduler.start()
        await _wait_until(lambda: handler.active == 1)
        first_expiry = _load_operations(database_engine, [operation_id])[0].lease_expires_at
        assert first_expiry is not None
        await asyncio.sleep(0.45)
        renewed_expiry = _load_operations(database_engine, [operation_id])[0].lease_expires_at
        assert renewed_expiry is not None
        assert renewed_expiry > first_expiry

        await scheduler.stop()
        stored = _load_operations(database_engine, [operation_id])[0]
        assert handler.cancelled == 1
        assert stored.status == OperationStatus.QUEUED.value
        assert stored.lease_owner is None
        assert stored.attempts == 1

        recovery = _scheduler(
            database_engine, ImmediateHandler(), ApiMetrics(), "recovery-scheduler"
        )
        await recovery.start()
        try:
            await _wait_until(
                lambda: _load_operations(database_engine, [operation_id])[0].status
                == OperationStatus.SUCCEEDED.value
            )
        finally:
            await recovery.stop()
        assert _load_operations(database_engine, [operation_id])[0].attempts == 2

    asyncio.run(exercise())


def test_expired_pod_lease_is_reclaimed(database_engine: Engine) -> None:
    operation_id = _create_operations(database_engine, 1)[0]
    factory = create_session_factory(database_engine)
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    with factory.begin() as session:
        claimed = claim_pending_operations(
            session,
            lease_owner="lost-pod",
            limit=1,
            lease_duration=timedelta(seconds=1),
        )
        assert claimed[0].id == operation_id
        session.execute(
            update(Operation)
            .where(Operation.id == operation_id)
            .values(lease_expires_at=expired_at)
        )

    async def exercise() -> None:
        metrics = ApiMetrics()
        scheduler = _scheduler(database_engine, ImmediateHandler(), metrics, "replacement-pod")
        await scheduler.start()
        try:
            await _wait_until(
                lambda: _load_operations(database_engine, [operation_id])[0].status
                == OperationStatus.SUCCEEDED.value
            )
        finally:
            await scheduler.stop()
        assert (
            metrics.registry.get_sample_value(
                "lap_platform_api_scheduler_claims_total",
                {"operation_type": "deploy", "claim_kind": "reclaimed"},
            )
            == 1
        )

    asyncio.run(exercise())
    stored = _load_operations(database_engine, [operation_id])[0]
    assert stored.attempts == 2
    assert stored.lease_owner is None


def test_success_retry_and_terminal_outcomes_are_persisted(database_engine: Engine) -> None:
    success_id, terminal_id, retry_id = _create_operations(database_engine, 3)

    async def exercise() -> None:
        handler = OutcomeHandler(terminal_id, retry_id)
        metrics = ApiMetrics()
        scheduler = _scheduler(database_engine, handler, metrics, "outcome-scheduler")
        await scheduler.start()
        try:
            await _wait_until(
                lambda: (
                    (stored := _load_operations(database_engine, [retry_id])[0]).attempts == 2
                    and stored.status == OperationStatus.RUNNING.value
                )
            )
            retry_running = _load_operations(database_engine, [retry_id])[0]
            assert retry_running.failure_code == "TEST_RETRYABLE_FAILURE"
            assert retry_running.failure_result == {"retryable": True}
            handler.retry_second_attempt.set()
            await _wait_until(
                lambda: all(
                    operation.status
                    in {OperationStatus.SUCCEEDED.value, OperationStatus.FAILED.value}
                    for operation in _load_operations(
                        database_engine, [success_id, terminal_id, retry_id]
                    )
                )
            )
        finally:
            await scheduler.stop()

        assert (
            metrics.registry.get_sample_value(
                "lap_platform_api_scheduler_outcomes_total",
                {"operation_type": "deploy", "outcome": "retry"},
            )
            == 1
        )

    asyncio.run(exercise())

    stored = {
        operation.id: operation
        for operation in _load_operations(database_engine, [success_id, terminal_id, retry_id])
    }
    assert stored[success_id].status == OperationStatus.SUCCEEDED.value
    assert stored[success_id].completed_at is not None
    assert stored[retry_id].status == OperationStatus.SUCCEEDED.value
    assert stored[retry_id].attempts == 2
    assert stored[retry_id].failure_code is None
    assert stored[terminal_id].status == OperationStatus.FAILED.value
    assert stored[terminal_id].failure_code == "TEST_TERMINAL_FAILURE"
    assert stored[terminal_id].failure_result == {"retryable": False}
    assert stored[terminal_id].completed_at is not None


def test_lease_loss_cancels_handler_without_overwriting_new_owner(
    database_engine: Engine,
) -> None:
    operation_id = _create_operations(database_engine, 1)[0]

    async def exercise() -> None:
        handler = BlockingHandler()
        metrics = ApiMetrics()
        scheduler = _scheduler(
            database_engine,
            handler,
            metrics,
            "old-owner",
            lease_seconds=0.4,
            renewal_seconds=0.05,
        )
        await scheduler.start()
        await _wait_until(lambda: handler.active == 1)
        factory = create_session_factory(database_engine)
        with factory.begin() as session:
            session.execute(
                update(Operation)
                .where(Operation.id == operation_id)
                .values(
                    lease_owner="new-owner",
                    lease_expires_at=datetime.now(UTC) + timedelta(seconds=1),
                )
            )
        await _wait_until(lambda: handler.cancelled == 1)
        await scheduler.stop()

        stored = _load_operations(database_engine, [operation_id])[0]
        assert stored.status == OperationStatus.RUNNING.value
        assert stored.lease_owner == "new-owner"
        assert stored.completed_at is None
        assert (
            metrics.registry.get_sample_value(
                "lap_platform_api_scheduler_lease_renewal_failures_total",
                {"operation_type": "deploy"},
            )
            == 1
        )

    asyncio.run(exercise())
