from __future__ import annotations

import secrets
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from lap_platform_api.app import AppDependencies, create_app
from lap_platform_api.auth import hash_bearer_token
from lap_platform_api.build_agent import BuildAgentClient
from lap_platform_api.config import (
    ApiSettings,
    BuildAgentSettings,
    KubernetesSettings,
    SchedulerSettings,
)
from lap_platform_api.database import DatabaseSettings, create_session_factory
from lap_platform_api.health import ReadinessChecker
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import create_engine

TOKEN = secrets.token_urlsafe(32)
REQUEST_ID = "00000000-0000-4000-8000-000000000001"


class Probe(Protocol):
    name: str

    def check(self) -> None: ...


class SuccessfulProbe:
    name = "postgresql"

    def __init__(self) -> None:
        self.calls = 0

    def check(self) -> None:
        self.calls += 1


class FailingProbe:
    name = "postgresql"

    def check(self) -> None:
        raise RuntimeError("database unavailable")


def settings(tmp_path: Path) -> ApiSettings:
    return ApiSettings(
        database=DatabaseSettings(
            host="postgresql",
            database="platform",
            username="platform_api",
            password="".join(("not-used", "-by-unit-tests")),
        ),
        build_agent=BuildAgentSettings(
            url="https://build-agent.lab.test:8443",
            ca_certificate=tmp_path / "ca.crt",
            client_certificate=tmp_path / "client.crt",
            client_key=tmp_path / "client.key",
        ),
        kubernetes=KubernetesSettings(enabled=False),
        scheduler=SchedulerSettings(enabled=False),
        token_sha256=hash_bearer_token(TOKEN),
        readiness_cache_seconds=5,
    )


def dependencies(configuration: ApiSettings, probe: Probe) -> AppDependencies:
    engine = create_engine("sqlite://")
    async_client = httpx.AsyncClient(
        base_url=configuration.build_agent.url,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    )
    return AppDependencies(
        engine=engine,
        session_factory=create_session_factory(engine),
        readiness=ReadinessChecker([probe], cache_seconds=5),
        build_agent=BuildAgentClient(configuration.build_agent, client=async_client),
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    configuration = settings(tmp_path)
    return TestClient(create_app(configuration, dependencies(configuration, SuccessfulProbe())))


@pytest.mark.unit
def test_liveness_is_anonymous_and_process_only(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.unit
def test_readiness_success_is_cached(tmp_path: Path) -> None:
    configuration = settings(tmp_path)
    probe = SuccessfulProbe()
    with TestClient(create_app(configuration, dependencies(configuration, probe))) as client:
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/readyz").json() == {"status": "ready"}
    assert probe.calls == 1


@pytest.mark.unit
def test_readiness_failure_uses_problem_details(tmp_path: Path) -> None:
    configuration = settings(tmp_path)
    with TestClient(
        create_app(configuration, dependencies(configuration, FailingProbe()))
    ) as client:
        response = client.get("/readyz", headers={"X-Request-ID": REQUEST_ID})
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "https://local-application-platform.dev/problems/DEPENDENCY_UNAVAILABLE",
        "title": "Service Unavailable",
        "status": 503,
        "detail": "one or more required dependencies are unavailable",
        "instance": "/readyz",
        "errorCode": "DEPENDENCY_UNAVAILABLE",
        "correlationId": REQUEST_ID,
        "extensions": {"dependencies": ["postgresql"]},
    }


@pytest.mark.unit
def test_protected_route_requires_valid_bearer_token(client: TestClient) -> None:
    missing = client.get("/api/v1/info")
    invalid = client.get("/api/v1/info", headers={"Authorization": "Bearer incorrect"})
    valid = client.get("/api/v1/info", headers={"Authorization": f"Bearer {TOKEN}"})

    for rejected in (missing, invalid):
        assert rejected.status_code == 401
        assert rejected.headers["www-authenticate"] == "Bearer"
        assert rejected.json()["errorCode"] == "AUTHENTICATION_FAILED"
        assert rejected.headers["content-type"].startswith("application/problem+json")
    assert valid.status_code == 200
    assert valid.json() == {"service": "platform-api", "apiVersion": "v1", "version": "1.0.0"}


@pytest.mark.unit
def test_request_id_is_preserved_and_returned(client: TestClient) -> None:
    response = client.get(
        "/api/v1/info",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Request-ID": REQUEST_ID},
    )
    assert response.headers["x-request-id"] == REQUEST_ID

    generated = client.get("/healthz")
    assert UUID(generated.headers["x-request-id"]).version == 4


@pytest.mark.unit
def test_invalid_request_id_is_problem_detail(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "not-a-uuid"})
    assert response.status_code == 400
    assert response.json()["errorCode"] == "REQUEST_ID_INVALID"
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.unit
def test_not_found_is_problem_detail(client: TestClient) -> None:
    response = client.get("/missing", headers={"X-Request-ID": REQUEST_ID})
    assert response.status_code == 404
    assert response.json()["errorCode"] == "RESOURCE_NOT_FOUND"
    assert response.json()["correlationId"] == REQUEST_ID


@pytest.mark.unit
def test_metrics_are_prometheus_compatible(client: TestClient) -> None:
    client.get("/healthz")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "lap_platform_api_http_requests_total" in response.text
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST


@pytest.mark.unit
def test_openapi_describes_authenticated_version_one_api(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    assert paths == {
        "/api/v1/applications",
        "/api/v1/applications/{name}",
        "/api/v1/applications/{name}/builds",
        "/api/v1/applications/{name}/deployments",
        "/api/v1/applications/{name}/operations",
        "/api/v1/applications/{name}/releases",
        "/api/v1/applications/{name}/rollbacks",
        "/api/v1/builds/{build_id}",
        "/api/v1/deployments",
        "/api/v1/deployments/{deployment_id}",
        "/api/v1/info",
        "/api/v1/operations/{operation_id}",
        "/api/v1/releases/{release_id}",
    }
    assert schema["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert schema["paths"]["/api/v1/info"]["get"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/api/v1/deployments"]["post"]["security"] == [{"HTTPBearer": []}]
