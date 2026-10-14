from __future__ import annotations

from typing import cast

import pytest
from lap_platform_api.health import PostgreSQLProbe, ReadinessChecker
from sqlalchemy import Engine, create_engine


class CountingProbe:
    name = "dependency"

    def __init__(self) -> None:
        self.calls = 0

    def check(self) -> None:
        self.calls += 1


@pytest.mark.unit
def test_postgresql_probe_executes_connectivity_query() -> None:
    engine = create_engine("sqlite://")
    PostgreSQLProbe(engine).check()
    engine.dispose()


@pytest.mark.unit
def test_postgresql_probe_propagates_connectivity_failure() -> None:
    class FailingEngine:
        def connect(self) -> None:
            raise RuntimeError("unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        PostgreSQLProbe(cast("Engine", FailingEngine())).check()


@pytest.mark.unit
def test_readiness_cache_can_be_invalidated() -> None:
    probe = CountingProbe()
    checker = ReadinessChecker([probe], cache_seconds=30)
    assert checker.check().ready
    assert checker.check().ready
    assert probe.calls == 1
    checker.invalidate()
    assert checker.check().ready
    assert probe.calls == 2
