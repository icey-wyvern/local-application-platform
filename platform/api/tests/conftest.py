from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from lap_platform_api.database import (
    DatabaseSettings,
    create_database_engine,
    create_session_factory,
)
from sqlalchemy import Engine
from sqlalchemy.orm import Session


def database_tests_enabled() -> bool:
    return os.environ.get("LAP_RUN_DATABASE_TESTS") == "1"


@pytest.fixture(scope="session")
def database_engine() -> Iterator[Engine]:
    if not database_tests_enabled():
        pytest.skip("set LAP_RUN_DATABASE_TESTS=1 with a disposable PostgreSQL database")
    engine = create_database_engine(DatabaseSettings.from_environment())
    yield engine
    engine.dispose()


@pytest.fixture
def session(database_engine: Engine) -> Iterator[Session]:
    connection = database_engine.connect()
    transaction = connection.begin()
    factory = create_session_factory(database_engine)
    database_session = factory(bind=connection)
    try:
        yield database_session
    finally:
        database_session.close()
        transaction.rollback()
        connection.close()
