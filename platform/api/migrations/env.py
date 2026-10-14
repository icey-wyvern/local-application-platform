from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from lap_platform_api.database import DatabaseSettings
from lap_platform_api.models import Base
from sqlalchemy import URL, Connection, create_engine, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def offline_database_url() -> URL:
    settings = DatabaseSettings.from_environment()
    return URL.create(
        "postgresql+psycopg",
        username=settings.username,
        host=settings.host,
        port=settings.port,
        database=settings.database,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=offline_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if isinstance(supplied_connection, Connection):
        context.configure(
            connection=supplied_connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = create_engine(
        DatabaseSettings.from_environment().sqlalchemy_url(), poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
