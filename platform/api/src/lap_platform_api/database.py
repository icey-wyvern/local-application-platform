from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    database: str
    username: str
    password: str
    port: int = 5432
    ssl_root_cert: Path | None = None
    host_address: str | None = None
    schema: str | None = None

    @classmethod
    def from_environment(cls) -> DatabaseSettings:
        root_cert = os.environ.get("POSTGRESQL_SSL_ROOT_CERT")
        schema = os.environ.get("POSTGRESQL_SCHEMA")
        if schema is not None and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema) is None:
            raise ValueError("POSTGRESQL_SCHEMA must be a safe PostgreSQL identifier")
        return cls(
            host=_required_environment("POSTGRESQL_HOST"),
            port=int(os.environ.get("POSTGRESQL_PORT", "5432")),
            database=_required_environment("POSTGRESQL_DATABASE"),
            username=_required_environment("POSTGRESQL_APPLICATION_USER"),
            password=_required_environment("POSTGRESQL_PASSWORD"),
            ssl_root_cert=Path(root_cert) if root_cert else None,
            host_address=os.environ.get("POSTGRESQL_HOST_ADDRESS"),
            schema=schema,
        )

    def sqlalchemy_url(self) -> URL:
        query: dict[str, str] = {}
        if self.ssl_root_cert is not None:
            query = {"sslmode": "verify-full", "sslrootcert": str(self.ssl_root_cert)}
        if self.host_address is not None:
            query["hostaddr"] = self.host_address
        if self.schema is not None:
            query["options"] = f"-csearch_path={self.schema},public"
        return URL.create(
            "postgresql+psycopg",
            username=self.username,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
            query=query,
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} must be set")
    return value


def create_database_engine(
    settings: DatabaseSettings,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
) -> Engine:
    return create_engine(
        settings.sqlalchemy_url(),
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory.begin() as session:
        yield session
