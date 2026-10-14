from __future__ import annotations

import ssl
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pytest
from lap_platform_api.build_agent import (
    BuildAgentClient,
    BuildAgentError,
    BuildRequest,
    Revision,
    RevisionType,
    create_mtls_context,
)
from lap_platform_api.config import BuildAgentSettings

pytestmark = pytest.mark.unit


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def settings(tmp_path: Path) -> BuildAgentSettings:
    return BuildAgentSettings(
        url="https://build-agent.lab.test:8443",
        ca_certificate=tmp_path / "ca.crt",
        client_certificate=tmp_path / "client.crt",
        client_key=tmp_path / "client.key",
        connect_timeout_seconds=5,
        request_timeout_seconds=920,
    )


def request() -> BuildRequest:
    return BuildRequest(
        buildId=UUID("00000000-0000-4000-8000-000000000001"),
        repositoryUrl="https://example.com/repository.git",
        revision=Revision(type=RevisionType.BRANCH, value="main"),
    )


@pytest.mark.unit
def test_mtls_context_uses_ca_and_client_keypair(tmp_path: Path) -> None:
    configuration = settings(tmp_path)
    context = MagicMock(spec=ssl.SSLContext)
    with patch("ssl.create_default_context", return_value=context) as create_context:
        assert create_mtls_context(configuration) is context
    create_context.assert_called_once_with(cafile=str(configuration.ca_certificate))
    context.load_cert_chain.assert_called_once_with(
        certfile=str(configuration.client_certificate), keyfile=str(configuration.client_key)
    )


@pytest.mark.anyio
async def test_typed_build_result_and_health(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64

    def handler(received: httpx.Request) -> httpx.Response:
        if received.url.path == "/healthz":
            return httpx.Response(200)
        assert received.url.path == "/v1/builds"
        return httpx.Response(
            200,
            json={
                "buildId": "00000000-0000-4000-8000-000000000001",
                "status": "succeeded",
                "commit": "b" * 40,
                "imageReference": f"registry.lab.test:5000/apps/example@{digest}",
                "imageDigest": digest,
                "application": {"name": "example", "port": 8080, "healthPath": "/healthz"},
                "vulnerabilities": {"threshold": "CRITICAL", "counts": {}, "blocked": 0},
            },
        )

    async_client = httpx.AsyncClient(
        base_url="https://build-agent.lab.test:8443",
        transport=httpx.MockTransport(handler),
    )
    client = BuildAgentClient(settings(tmp_path), client=async_client)
    assert await client.health()
    result = await client.submit_build(request())
    assert result.imageDigest == digest
    await async_client.aclose()


@pytest.mark.anyio
async def test_typed_build_agent_error(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "image_rejected",
                    "message": "image failed policy",
                    "stage": "scan",
                    "retryable": False,
                }
            },
        )

    async_client = httpx.AsyncClient(
        base_url="https://build-agent.lab.test:8443",
        transport=httpx.MockTransport(handler),
    )
    client = BuildAgentClient(settings(tmp_path), client=async_client)
    with pytest.raises(BuildAgentError) as raised:
        await client.submit_build(request())
    assert raised.value.status_code == 422
    assert raised.value.detail.code == "image_rejected"
    await async_client.aclose()


def test_revision_rejects_unsafe_git_reference() -> None:
    with pytest.raises(ValueError, match="safe Git reference"):
        Revision(type=RevisionType.BRANCH, value="main..other")
