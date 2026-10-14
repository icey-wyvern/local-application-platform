import pytest
from lap_platform_api.states import (
    BUILD_TRANSITIONS,
    BuildStatus,
    InvalidStateTransition,
    validate_transition,
)


@pytest.mark.unit
def test_build_state_transition_is_explicit() -> None:
    validate_transition(BuildStatus.PENDING, BuildStatus.CLONING, BUILD_TRANSITIONS)

    with pytest.raises(InvalidStateTransition):
        validate_transition(BuildStatus.PENDING, BuildStatus.SUCCEEDED, BUILD_TRANSITIONS)
