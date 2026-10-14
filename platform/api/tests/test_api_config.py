from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from lap_platform_api.auth import hash_bearer_token
from lap_platform_api.config import ApiSettings, BuildAgentSettings


@pytest.mark.unit
def test_environment_uses_authoritative_database_and_tls_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = secrets.token_urlsafe(32)
    values = {
        "POSTGRESQL_HOST": "postgresql.platform-database.svc.cluster.local",
        "POSTGRESQL_DATABASE": "platform",
        "POSTGRESQL_APPLICATION_USER": "platform_api",
        "POSTGRESQL_PASSWORD": secrets.token_urlsafe(24),
        "POSTGRESQL_SSL_ROOT_CERT": "/run/platform/ca.crt",
        "BUILD_AGENT_URL": "https://build-agent.lab.test:8443",
        "BUILD_AGENT_CA_CERTIFICATE": "/run/platform/build-agent/ca.crt",
        "BUILD_AGENT_CLIENT_CERTIFICATE": "/run/platform/build-agent/tls.crt",
        "BUILD_AGENT_CLIENT_KEY": "/run/platform/build-agent/tls.key",
        "LAP_API_TOKEN_SHA256": hash_bearer_token(token),
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
        "LAP_SCHEDULER_POLL_INTERVAL_SECONDS": "2",
        "LAP_SCHEDULER_RETRY_DELAY_SECONDS": "3",
        "LAP_SCHEDULER_NOOP_DELAY_SECONDS": "4",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = ApiSettings.from_environment()

    assert settings.database.host == values["POSTGRESQL_HOST"]
    assert settings.database.ssl_root_cert == Path(values["POSTGRESQL_SSL_ROOT_CERT"])
    assert settings.build_agent.url == values["BUILD_AGENT_URL"]
    assert settings.token_sha256 == hash_bearer_token(token)
    assert settings.scheduler.enabled is True
    assert settings.scheduler.poll_interval_seconds == 2
    assert settings.scheduler.retry_delay_seconds == 3
    assert settings.scheduler.noop_delay_seconds == 4
    assert token not in settings.model_dump_json()


@pytest.mark.unit
def test_build_agent_configuration_requires_https(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        BuildAgentSettings(
            url="http://build-agent.lab.test:8443",
            ca_certificate=tmp_path / "ca.crt",
            client_certificate=tmp_path / "client.crt",
            client_key=tmp_path / "client.key",
        )
