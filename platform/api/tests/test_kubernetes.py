from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from lap_platform_api.config import KubernetesSettings
from lap_platform_api.kubernetes import (
    ApplicationClient,
    DesiredApplication,
    KubernetesApplicationRejectedError,
    KubernetesApplicationRolloutError,
    KubernetesApplicationTransientError,
)

NAME = "example-api"
RELEASE = UUID("00000000-0000-4000-8000-000000000001")
IMAGE = "registry.lab.test:5000/platform/example-api@sha256:" + "d" * 64


def _settings() -> KubernetesSettings:
    return KubernetesSettings(
        enabled=True,
        ca_certificate=Path("unused"),
        token_file=Path("unused"),
        observation_timeout_seconds=0.02,
        observation_poll_seconds=0.001,
    )


def _desired() -> DesiredApplication:
    return DesiredApplication(NAME, RELEASE, IMAGE, 8080, "/healthz")


def _resource(
    *,
    release: UUID = RELEASE,
    generation: int = 1,
    resource_version: str = "10",
    reconciled: str | None = None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "apiVersion": "platform.lab.test/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": NAME,
            "generation": generation,
            "resourceVersion": resource_version,
        },
        "spec": {
            "releaseId": str(release),
            "image": IMAGE,
            "port": 8080,
            "healthPath": "/healthz",
            "replicas": 2,
        },
    }
    if reconciled is not None:
        document["status"] = {
            "observedGeneration": generation,
            "desiredImage": IMAGE,
            "conditions": [
                {
                    "type": "Reconciled",
                    "status": reconciled,
                    "reason": "RuntimeResourcesReconciled"
                    if reconciled == "True"
                    else "InvalidSpec",
                    "message": "accepted" if reconciled == "True" else "rejected",
                    "observedGeneration": generation,
                },
                {
                    "type": "Available",
                    "status": "False",
                    "reason": "ApplicationProgressing",
                    "observedGeneration": generation,
                },
            ],
        }
    return document


@pytest.mark.unit
def test_create_uses_exact_application_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        created = _resource()
        created["spec"] = json.loads(request.content)["spec"]
        return httpx.Response(201, json=created)

    client = ApplicationClient(
        _settings(),
        client=httpx.AsyncClient(
            base_url="https://kubernetes.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    result = asyncio.run(client.apply(_desired(), None))

    assert result.action == "created"
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == _desired().resource_document()
    assert set(json.loads(requests[0].content)["spec"]) == {
        "releaseId",
        "image",
        "port",
        "healthPath",
        "replicas",
    }


@pytest.mark.unit
def test_update_is_resource_version_guarded_and_idempotent() -> None:
    current_release = UUID("00000000-0000-4000-8000-000000000002")
    current_document = _resource(release=current_release)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        updated = _resource(generation=2, resource_version="11")
        return httpx.Response(200, json=updated)

    client = ApplicationClient(
        _settings(),
        client=httpx.AsyncClient(
            base_url="https://kubernetes.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    current = client._parse(httpx.Response(200, json=current_document))
    result = asyncio.run(client.apply(_desired(), current))
    repeated = asyncio.run(client.apply(_desired(), result.resource))

    patch = json.loads(requests[0].content)
    assert result.action == "updated"
    assert repeated.action == "unchanged"
    assert len(requests) == 1
    assert requests[0].headers["content-type"] == "application/json-patch+json"
    assert patch[0] == {
        "op": "test",
        "path": "/metadata/resourceVersion",
        "value": "10",
    }
    assert patch[1]["value"] == _desired().resource_document()["spec"]


@pytest.mark.unit
def test_observation_stops_at_reconciled_without_waiting_for_available() -> None:
    async def exercise() -> None:
        client = ApplicationClient(
            _settings(),
            client=httpx.AsyncClient(
                base_url="https://kubernetes.test",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json=_resource(reconciled="True"))
                ),
            ),
        )
        observed = await client.wait_until_observed(_desired(), 1, asyncio.Event())
        assert observed.status is not None
        available = next(c for c in observed.status.conditions if c.type == "Available")
        assert available.status == "False"

    asyncio.run(exercise())


@pytest.mark.unit
def test_rejection_is_terminal_and_conflict_is_retryable() -> None:
    async def rejected() -> None:
        client = ApplicationClient(
            _settings(),
            client=httpx.AsyncClient(
                base_url="https://kubernetes.test",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json=_resource(reconciled="False"))
                ),
            ),
        )
        with pytest.raises(KubernetesApplicationRejectedError, match="rejected"):
            await client.wait_until_observed(_desired(), 1, asyncio.Event())

    async def conflict() -> None:
        client = ApplicationClient(
            _settings(),
            client=httpx.AsyncClient(
                base_url="https://kubernetes.test",
                transport=httpx.MockTransport(lambda _request: httpx.Response(409)),
            ),
        )
        with pytest.raises(KubernetesApplicationTransientError):
            await client.apply(_desired(), None)

    asyncio.run(rejected())
    asyncio.run(conflict())


@pytest.mark.unit
def test_full_acceptance_requires_all_controller_gates_and_verified_https() -> None:
    document = _resource(reconciled="True")
    document["status"] = {
        "observedGeneration": 1,
        "activeReleaseId": str(RELEASE),
        "desiredImage": IMAGE,
        "currentImage": IMAGE,
        "readyReplicas": 2,
        "phase": "Ready",
        "conditions": [
            {
                "type": condition,
                "status": "True",
                "reason": reason,
                "observedGeneration": 1,
            }
            for condition, reason in (
                ("Reconciled", "RuntimeResourcesReconciled"),
                ("RuntimeReady", "WorkloadAndServiceReady"),
                ("RouteReady", "RouteAccepted"),
                ("Healthy", "ExternalHealthCheckSucceeded"),
                ("Available", "ApplicationHealthy"),
            )
        ],
    }
    health_requests: list[httpx.Request] = []

    def health_handler(request: httpx.Request) -> httpx.Response:
        health_requests.append(request)
        return httpx.Response(204)

    async def exercise() -> None:
        client = ApplicationClient(
            _settings(),
            client=httpx.AsyncClient(
                base_url="https://kubernetes.test",
                transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=document)),
            ),
            health_client=httpx.AsyncClient(transport=httpx.MockTransport(health_handler)),
        )
        ready = await client.wait_until_ready(_desired(), 1, asyncio.Event(), 0.1)
        await client.verify_external_health(_desired(), asyncio.Event(), 0.1)
        assert ready.status is not None and ready.status.ready_replicas == 2

    asyncio.run(exercise())
    assert str(health_requests[0].url) == "https://example-api.apps.lab.test/healthz"


@pytest.mark.unit
def test_route_rejection_and_https_failure_have_machine_codes() -> None:
    route_rejected = _resource(reconciled="True")
    route_rejected["status"] = {
        "observedGeneration": 1,
        "desiredImage": IMAGE,
        "phase": "Error",
        "conditions": [
            {
                "type": "RouteReady",
                "status": "False",
                "reason": "RouteNotAccepted",
                "message": "listener rejected route",
                "observedGeneration": 1,
            }
        ],
    }

    async def exercise() -> None:
        client = ApplicationClient(
            _settings(),
            client=httpx.AsyncClient(
                base_url="https://kubernetes.test",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json=route_rejected)
                ),
            ),
            health_client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _request: httpx.Response(503))
            ),
        )
        with pytest.raises(KubernetesApplicationRolloutError) as route_error:
            await client.wait_until_ready(_desired(), 1, asyncio.Event(), 0.1)
        assert route_error.value.code == "HTTP_ROUTE_NOT_ACCEPTED"
        with pytest.raises(KubernetesApplicationRolloutError) as health_error:
            await client.verify_external_health(_desired(), asyncio.Event(), 0.002)
        assert health_error.value.code == "EXTERNAL_HTTPS_HEALTH_FAILED"

    asyncio.run(exercise())


@pytest.mark.unit
def test_delete_waits_for_controller_cleanup_and_rejects_api_errors() -> None:
    requests: list[httpx.Request] = []
    reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(202)
        reads += 1
        return httpx.Response(200, json=_resource()) if reads == 1 else httpx.Response(404)

    async def exercise() -> None:
        client = ApplicationClient(
            _settings(),
            client=httpx.AsyncClient(
                base_url="https://kubernetes.test", transport=httpx.MockTransport(handler)
            ),
        )
        await client.delete(NAME)
        await client.wait_until_deleted(NAME, asyncio.Event())
        assert [request.method for request in requests] == ["DELETE", "GET", "GET"]
        assert requests[0].url.path.endswith(f"/applications/{NAME}")

        rejected = ApplicationClient(
            _settings(),
            client=httpx.AsyncClient(
                base_url="https://kubernetes.test",
                transport=httpx.MockTransport(lambda _: httpx.Response(403)),
            ),
        )
        with pytest.raises(KubernetesApplicationRejectedError):
            await rejected.delete(NAME)

        pending = ApplicationClient(
            _settings().model_copy(update={"deployment_timeout_seconds": 0.002}),
            client=httpx.AsyncClient(
                base_url="https://kubernetes.test",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_resource())),
            ),
        )
        with pytest.raises(KubernetesApplicationTransientError, match="deletion did not complete"):
            await pending.wait_until_deleted(NAME, asyncio.Event())

    asyncio.run(exercise())
