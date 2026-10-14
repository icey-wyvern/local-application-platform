from __future__ import annotations

import logging
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import Engine, text

from lap_platform_api.config import KubernetesSettings

LOGGER = logging.getLogger(__name__)


class DependencyProbe(Protocol):
    name: str

    def check(self) -> None: ...


class PostgreSQLProbe:
    name = "postgresql"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def check(self) -> None:
        with self._engine.connect() as connection:
            connection.execute(text("SELECT 1"))


class KubernetesProbe:
    name = "kubernetes"

    def __init__(self, settings: KubernetesSettings) -> None:
        self._settings = settings

    def check(self) -> None:
        token = self._settings.token_file.read_text(encoding="utf-8").strip()
        context = ssl.create_default_context(cafile=str(self._settings.ca_certificate))
        url = f"https://{self._settings.host}:{self._settings.port}/version"
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            verify=context,
            timeout=self._settings.timeout_seconds,
        )
        response.raise_for_status()


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    failures: tuple[str, ...]


class ReadinessChecker:
    def __init__(self, probes: list[DependencyProbe], cache_seconds: float = 5.0) -> None:
        self._probes = probes
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: ReadinessResult | None = None

    def check(self) -> ReadinessResult:
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now - self._cached_at < self._cache_seconds:
                return self._cached
            failures: list[str] = []
            for probe in self._probes:
                try:
                    probe.check()
                except Exception:
                    LOGGER.warning(
                        "readiness dependency unavailable",
                        extra={"dependency": probe.name},
                    )
                    failures.append(probe.name)
            result = ReadinessResult(ready=not failures, failures=tuple(failures))
            self._cached = result
            self._cached_at = now
            return result

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
            self._cached_at = 0.0
