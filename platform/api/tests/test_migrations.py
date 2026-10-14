from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, inspect

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[1]


def _config(connection: Connection) -> Config:
    config = Config(API_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_upgrade_from_empty_and_downgrade_reupgrade(database_engine: Engine) -> None:
    with database_engine.connect() as connection:
        config = _config(connection)
        command.downgrade(config, "base")
        connection.commit()
        assert inspect(connection).get_table_names() == ["alembic_version"]

        command.upgrade(config, "head")
        connection.commit()
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "applications",
            "audit_events",
            "builds",
            "deployments",
            "operations",
            "releases",
        }

        command.check(config)
