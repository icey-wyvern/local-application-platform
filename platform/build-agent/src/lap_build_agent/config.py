from __future__ import annotations

from dataclasses import dataclass
from os import environ
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    listen_host: str
    listen_port: int
    state_path: Path
    buildkit_socket: Path
    trivy_cache_path: Path
    registry: str
    registry_namespace: str
    registry_auth_file: Path
    vulnerability_threshold: str
    build_timeout_seconds: int
    otlp_endpoint: str | None

    @classmethod
    def from_environment(cls) -> Settings:
        state_path = Path(environ.get("LAP_BUILD_AGENT_STATE_PATH", "/var/lib/lap-build-agent"))
        return cls(
            listen_host=environ.get("LAP_BUILD_AGENT_LISTEN_HOST", "127.0.0.1"),
            listen_port=int(environ.get("LAP_BUILD_AGENT_LISTEN_PORT", "8443")),
            state_path=state_path,
            buildkit_socket=Path(
                environ.get("LAP_BUILDKIT_SOCKET", "/run/lap-buildkit/buildkitd.sock")
            ),
            trivy_cache_path=Path(environ.get("LAP_TRIVY_CACHE_PATH", "/var/cache/lap-trivy")),
            registry=environ.get("LAP_REGISTRY", "registry.lab.test:5000"),
            registry_namespace=environ.get("LAP_REGISTRY_NAMESPACE", "apps"),
            registry_auth_file=Path(
                environ.get(
                    "LAP_REGISTRY_AUTH_FILE",
                    str(state_path / "registry-auth.json"),
                )
            ),
            vulnerability_threshold=environ.get("LAP_VULNERABILITY_THRESHOLD", "CRITICAL").upper(),
            build_timeout_seconds=int(environ.get("LAP_BUILD_TIMEOUT_SECONDS", "900")),
            otlp_endpoint=environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        )
