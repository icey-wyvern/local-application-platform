from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lap_platform_api.database import DatabaseSettings


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} must be set")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean")


class BuildAgentSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    ca_certificate: Path
    client_certificate: Path
    client_key: Path
    connect_timeout_seconds: Annotated[float, Field(gt=0)] = 5.0
    request_timeout_seconds: Annotated[float, Field(gt=0)] = 920.0

    @field_validator("url")
    @classmethod
    def require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("build-agent URL must use HTTPS")
        return value.rstrip("/")


class KubernetesSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    host: str = "kubernetes.default.svc"
    port: int = 443
    ca_certificate: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    token_file: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    timeout_seconds: Annotated[float, Field(gt=0)] = 2.0
    observation_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30.0
    observation_poll_seconds: Annotated[float, Field(gt=0, le=10)] = 0.5
    deployment_timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 300.0
    external_health_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 5.0
    application_domain: str = "apps.lab.test"

    @field_validator("application_domain")
    @classmethod
    def validate_application_domain(cls, value: str) -> str:
        normalized = value.strip().lower().rstrip(".")
        if not normalized or "/" in normalized or ":" in normalized:
            raise ValueError("application domain must be a DNS name")
        return normalized


class SchedulerSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    poll_interval_seconds: Annotated[float, Field(gt=0, le=10)] = 1.0
    retry_delay_seconds: Annotated[float, Field(ge=0, le=30)] = 1.0
    noop_delay_seconds: Annotated[float, Field(ge=0, le=30)] = 0.0


class ApiSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    database: DatabaseSettings
    build_agent: BuildAgentSettings
    kubernetes: KubernetesSettings
    scheduler: SchedulerSettings = SchedulerSettings()
    token_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    service_version: str = "1.0.0"
    readiness_cache_seconds: Annotated[float, Field(ge=0, le=30)] = 5.0
    otlp_endpoint: str | None = None
    log_level: str = "INFO"

    @classmethod
    def from_environment(cls) -> ApiSettings:
        return cls(
            database=DatabaseSettings.from_environment(),
            build_agent=BuildAgentSettings(
                url=_required("BUILD_AGENT_URL"),
                ca_certificate=Path(_required("BUILD_AGENT_CA_CERTIFICATE")),
                client_certificate=Path(_required("BUILD_AGENT_CLIENT_CERTIFICATE")),
                client_key=Path(_required("BUILD_AGENT_CLIENT_KEY")),
                connect_timeout_seconds=float(
                    os.environ.get("BUILD_AGENT_CONNECT_TIMEOUT_SECONDS", "5")
                ),
                request_timeout_seconds=float(
                    os.environ.get("BUILD_AGENT_REQUEST_TIMEOUT_SECONDS", "920")
                ),
            ),
            kubernetes=KubernetesSettings(
                enabled=_boolean("KUBERNETES_READINESS_ENABLED", True),
                host=os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
                port=int(os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")),
                ca_certificate=Path(
                    os.environ.get(
                        "KUBERNETES_CA_CERTIFICATE",
                        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                    )
                ),
                token_file=Path(
                    os.environ.get(
                        "KUBERNETES_TOKEN_FILE",
                        "/var/run/secrets/kubernetes.io/serviceaccount/token",
                    )
                ),
                timeout_seconds=float(os.environ.get("KUBERNETES_TIMEOUT_SECONDS", "2")),
                observation_timeout_seconds=float(
                    os.environ.get("KUBERNETES_OBSERVATION_TIMEOUT_SECONDS", "30")
                ),
                observation_poll_seconds=float(
                    os.environ.get("KUBERNETES_OBSERVATION_POLL_SECONDS", "0.5")
                ),
                deployment_timeout_seconds=float(
                    os.environ.get("KUBERNETES_DEPLOYMENT_TIMEOUT_SECONDS", "300")
                ),
                external_health_timeout_seconds=float(
                    os.environ.get("EXTERNAL_HEALTH_TIMEOUT_SECONDS", "5")
                ),
                application_domain=os.environ.get("APPLICATION_DOMAIN", "apps.lab.test"),
            ),
            scheduler=SchedulerSettings(
                enabled=_boolean("LAP_SCHEDULER_ENABLED", True),
                poll_interval_seconds=float(
                    os.environ.get("LAP_SCHEDULER_POLL_INTERVAL_SECONDS", "1")
                ),
                retry_delay_seconds=float(os.environ.get("LAP_SCHEDULER_RETRY_DELAY_SECONDS", "1")),
                noop_delay_seconds=float(os.environ.get("LAP_SCHEDULER_NOOP_DELAY_SECONDS", "0")),
            ),
            token_sha256=_required("LAP_API_TOKEN_SHA256"),
            listen_host=os.environ.get("LAP_API_LISTEN_HOST", "127.0.0.1"),
            listen_port=int(os.environ.get("LAP_API_LISTEN_PORT", "8080")),
            readiness_cache_seconds=float(os.environ.get("LAP_READINESS_CACHE_SECONDS", "5")),
            otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            log_level=os.environ.get("LAP_LOG_LEVEL", "INFO").upper(),
        )
