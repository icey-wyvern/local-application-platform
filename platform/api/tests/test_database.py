from __future__ import annotations

from pathlib import Path

import pytest
from lap_platform_api.database import DatabaseSettings
from sqlalchemy import URL


@pytest.mark.unit
def test_database_url_keeps_credentials_structured() -> None:
    credentials = "".join(("p@ss", ":/word"))
    settings = DatabaseSettings(
        host="postgresql.platform-database.svc.cluster.local",
        database="platform",
        username="platform_api",
        password=credentials,
        ssl_root_cert=Path("/run/secrets/lab-root-ca.crt"),
    )

    url = settings.sqlalchemy_url()

    assert isinstance(url, URL)
    assert url.password == credentials
    assert url.query == {
        "sslmode": "verify-full",
        "sslrootcert": str(Path("/run/secrets/lab-root-ca.crt")),
    }
    assert credentials not in url.render_as_string()


@pytest.mark.unit
def test_disposable_schema_name_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRESQL_HOST", "postgresql")
    monkeypatch.setenv("POSTGRESQL_DATABASE", "platform")
    monkeypatch.setenv("POSTGRESQL_APPLICATION_USER", "platform_api")
    monkeypatch.setenv("POSTGRESQL_PASSWORD", "not-a-real-secret")
    monkeypatch.setenv("POSTGRESQL_SCHEMA", "bad; DROP SCHEMA public")

    with pytest.raises(ValueError, match="safe PostgreSQL identifier"):
        DatabaseSettings.from_environment()
