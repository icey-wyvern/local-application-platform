from enum import StrEnum


class BuildStatus(StrEnum):
    PENDING = "pending"
    CLONING = "cloning"
    VALIDATING = "validating"
    BUILDING = "building"
    SCANNING = "scanning"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class DeploymentStatus(StrEnum):
    PENDING = "pending"
    APPLYING = "applying"
    RECONCILING = "reconciling"
    VERIFYING = "verifying"
    HEALTHY = "healthy"
    FAILED = "failed"


class DeploymentType(StrEnum):
    RELEASE = "release"
    MANUAL_ROLLBACK = "manual_rollback"
    AUTOMATIC_ROLLBACK = "automatic_rollback"


class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OperationType(StrEnum):
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    DELETE = "delete"


BUILD_TRANSITIONS: dict[BuildStatus, frozenset[BuildStatus]] = {
    BuildStatus.PENDING: frozenset({BuildStatus.CLONING, BuildStatus.FAILED}),
    BuildStatus.CLONING: frozenset({BuildStatus.VALIDATING, BuildStatus.FAILED}),
    BuildStatus.VALIDATING: frozenset({BuildStatus.BUILDING, BuildStatus.FAILED}),
    BuildStatus.BUILDING: frozenset({BuildStatus.SCANNING, BuildStatus.FAILED}),
    BuildStatus.SCANNING: frozenset({
        BuildStatus.PUBLISHING,
        BuildStatus.REJECTED,
        BuildStatus.FAILED,
    }),
    BuildStatus.PUBLISHING: frozenset({BuildStatus.SUCCEEDED, BuildStatus.FAILED}),
    BuildStatus.SUCCEEDED: frozenset(),
    BuildStatus.FAILED: frozenset(),
    BuildStatus.REJECTED: frozenset(),
}

DEPLOYMENT_TRANSITIONS: dict[DeploymentStatus, frozenset[DeploymentStatus]] = {
    DeploymentStatus.PENDING: frozenset({DeploymentStatus.APPLYING, DeploymentStatus.FAILED}),
    DeploymentStatus.APPLYING: frozenset({DeploymentStatus.RECONCILING, DeploymentStatus.FAILED}),
    DeploymentStatus.RECONCILING: frozenset({DeploymentStatus.VERIFYING, DeploymentStatus.FAILED}),
    DeploymentStatus.VERIFYING: frozenset({DeploymentStatus.HEALTHY, DeploymentStatus.FAILED}),
    DeploymentStatus.HEALTHY: frozenset(),
    DeploymentStatus.FAILED: frozenset(),
}

OPERATION_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.QUEUED: frozenset({OperationStatus.RUNNING, OperationStatus.FAILED}),
    OperationStatus.RUNNING: frozenset({
        OperationStatus.QUEUED,
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
    }),
    OperationStatus.SUCCEEDED: frozenset(),
    OperationStatus.FAILED: frozenset(),
}


class InvalidStateTransition(ValueError):
    pass


def validate_transition[State: StrEnum](
    current: State, target: State, allowed: dict[State, frozenset[State]]
) -> None:
    if target not in allowed[current]:
        raise InvalidStateTransition(f"cannot transition from {current.value} to {target.value}")
