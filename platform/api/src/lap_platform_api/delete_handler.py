"""Delete Application custom resources before hiding their durable identity."""

from __future__ import annotations

import asyncio
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from lap_platform_api.kubernetes import (
    KubernetesApplicationRejectedError,
    KubernetesApplicationTransientError,
)
from lap_platform_api.repositories import (
    OperationLeaseLostError,
    get_application,
    get_operation,
    soft_delete_for_operation,
)
from lap_platform_api.scheduler import OperationExecutionContext, OperationResult


class DeletionControlPlane(Protocol):
    async def delete(self, name: str) -> None: ...

    async def wait_until_deleted(self, name: str, lease_lost: asyncio.Event) -> None: ...


class DeleteApplicationHandler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        application_client: DeletionControlPlane,
    ) -> None:
        self._session_factory = session_factory
        self._application_client = application_client

    async def execute(self, context: OperationExecutionContext) -> OperationResult:
        with self._session_factory() as session:
            operation = get_operation(session, context.operation_id)
            application = (
                get_application(session, operation.application_id, include_deleted=True)
                if operation is not None and operation.application_id is not None
                else None
            )
            if application is None:
                return OperationResult.failed("APPLICATION_NOT_FOUND", "application was not found")
            name = application.name
        try:
            await self._application_client.delete(name)
            await self._application_client.wait_until_deleted(name, context.lease_lost)
            with self._session_factory.begin() as session:
                soft_delete_for_operation(
                    session, operation_id=context.operation_id, lease_owner=context.lease_owner
                )
        except (KubernetesApplicationTransientError, OperationLeaseLostError):
            return OperationResult.retry(
                "APPLICATION_DELETE_PENDING", "application deletion is pending"
            )
        except KubernetesApplicationRejectedError:
            return OperationResult.failed(
                "APPLICATION_DELETE_REJECTED", "Application deletion was rejected"
            )
        return OperationResult.succeeded()
