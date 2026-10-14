from __future__ import annotations

import asyncio
import json
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from lap_platform_api.config import KubernetesSettings

APPLICATIONS_PATH = "/apis/platform.lab.test/v1alpha1/applications"


class KubernetesApplicationError(RuntimeError):
    """Base failure returned by the Application custom-resource client."""


class KubernetesApplicationTransientError(KubernetesApplicationError):
    """A retry may safely converge the desired Application state."""


class KubernetesApplicationRejectedError(KubernetesApplicationError):
    """The API server or controller rejected the desired Application state."""


class KubernetesApplicationRolloutError(KubernetesApplicationRejectedError):
    """The desired Application could not reach a healthy runtime by its deadline."""

    def __init__(self, code: str, message: str, stage: str) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage


class ApplicationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    release_id: UUID = Field(alias="releaseId")
    image: str
    port: Annotated[int, Field(ge=1024, le=65535)]
    health_path: str = Field(alias="healthPath")
    replicas: Literal[2] = 2


class ObjectMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    generation: int = 0
    resource_version: str | None = Field(default=None, alias="resourceVersion")
    deletion_timestamp: str | None = Field(default=None, alias="deletionTimestamp")


class ApplicationCondition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    status: str
    reason: str = ""
    message: str = ""
    observed_generation: int = Field(default=0, alias="observedGeneration")


class ApplicationStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    observed_generation: int = Field(default=0, alias="observedGeneration")
    desired_image: str | None = Field(default=None, alias="desiredImage")
    active_release_id: UUID | None = Field(default=None, alias="activeReleaseId")
    current_image: str | None = Field(default=None, alias="currentImage")
    ready_replicas: int = Field(default=0, alias="readyReplicas")
    phase: str = ""
    message: str = ""
    conditions: tuple[ApplicationCondition, ...] = ()


class ApplicationResource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_version: str = Field(alias="apiVersion")
    kind: str
    metadata: ObjectMetadata
    spec: ApplicationSpec
    status: ApplicationStatus | None = None


@dataclass(frozen=True)
class DesiredApplication:
    name: str
    release_id: UUID
    image: str
    port: int
    health_path: str

    def spec(self) -> ApplicationSpec:
        return ApplicationSpec(
            releaseId=self.release_id,
            image=self.image,
            port=self.port,
            healthPath=self.health_path,
            replicas=2,
        )

    def resource_document(self) -> dict[str, object]:
        return {
            "apiVersion": "platform.lab.test/v1alpha1",
            "kind": "Application",
            "metadata": {"name": self.name},
            "spec": self.spec().model_dump(mode="json", by_alias=True),
        }


@dataclass(frozen=True)
class ApplicationApplyResult:
    resource: ApplicationResource
    action: Literal["created", "updated", "unchanged"]


class ApplicationClient:
    """Minimal typed client for the API-owned Application spec boundary."""

    def __init__(
        self,
        settings: KubernetesSettings,
        *,
        client: httpx.AsyncClient | None = None,
        health_ca_certificate: Path | None = None,
        health_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        if client is None:
            token = settings.token_file.read_text(encoding="utf-8").strip()
            context = ssl.create_default_context(cafile=str(settings.ca_certificate))
            client = httpx.AsyncClient(
                base_url=f"https://{settings.host}:{settings.port}",
                headers={"Authorization": f"Bearer {token}"},
                verify=context,
                timeout=settings.timeout_seconds,
            )
        self._client = client
        self._owns_health_client = health_client is None and health_ca_certificate is not None
        if health_client is None and health_ca_certificate is not None:
            health_context = ssl.create_default_context(cafile=str(health_ca_certificate))
            health_client = httpx.AsyncClient(
                verify=health_context,
                timeout=settings.external_health_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            )
        self._health_client = health_client

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._owns_health_client:
            assert self._health_client is not None
            await self._health_client.aclose()

    async def get(self, name: str) -> ApplicationResource | None:
        try:
            response = await self._client.get(f"{APPLICATIONS_PATH}/{name}")
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise KubernetesApplicationTransientError("Kubernetes API request failed") from error
        if response.status_code == 404:
            return None
        self._raise_for_response(response)
        return self._parse(response)

    async def delete(self, name: str) -> None:
        try:
            response = await self._client.delete(f"{APPLICATIONS_PATH}/{name}")
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise KubernetesApplicationTransientError("Kubernetes API request failed") from error
        if response.status_code != 404:
            self._raise_for_response(response)

    async def wait_until_deleted(self, name: str, lease_lost: asyncio.Event) -> None:
        deadline = asyncio.get_running_loop().time() + self._settings.deployment_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if lease_lost.is_set():
                raise KubernetesApplicationTransientError("operation lease was lost")
            if await self.get(name) is None:
                return
            await asyncio.sleep(self._settings.observation_poll_seconds)
        raise KubernetesApplicationTransientError(
            "Application deletion did not complete before timeout"
        )

    async def apply(
        self,
        desired: DesiredApplication,
        current: ApplicationResource | None,
    ) -> ApplicationApplyResult:
        spec = desired.spec()
        if current is not None and current.metadata.deletion_timestamp is not None:
            raise KubernetesApplicationRejectedError("Application is being deleted")
        if current is not None and current.spec == spec:
            return ApplicationApplyResult(current, "unchanged")
        try:
            if current is None:
                response = await self._client.post(
                    APPLICATIONS_PATH,
                    json=desired.resource_document(),
                )
                action: Literal["created", "updated", "unchanged"] = "created"
            else:
                resource_version = current.metadata.resource_version
                if resource_version is None:
                    raise KubernetesApplicationTransientError(
                        "Application resourceVersion is missing"
                    )
                patch = [
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": resource_version,
                    },
                    {
                        "op": "replace",
                        "path": "/spec",
                        "value": spec.model_dump(mode="json", by_alias=True),
                    },
                ]
                response = await self._client.patch(
                    f"{APPLICATIONS_PATH}/{desired.name}",
                    content=json.dumps(patch, separators=(",", ":")),
                    headers={"Content-Type": "application/json-patch+json"},
                )
                action = "updated"
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise KubernetesApplicationTransientError("Kubernetes API request failed") from error
        self._raise_for_response(response)
        return ApplicationApplyResult(self._parse(response), action)

    async def wait_until_observed(
        self,
        desired: DesiredApplication,
        generation: int,
        lease_lost: asyncio.Event,
    ) -> ApplicationResource:
        deadline = asyncio.get_running_loop().time() + self._settings.observation_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if lease_lost.is_set():
                raise KubernetesApplicationTransientError("operation lease was lost")
            current = await self.get(desired.name)
            if current is not None:
                if current.metadata.deletion_timestamp is not None:
                    raise KubernetesApplicationRejectedError("Application is being deleted")
                if current.spec.release_id != desired.release_id:
                    raise KubernetesApplicationTransientError(
                        "Application desired release changed while awaiting observation"
                    )
                status = current.status
                if (
                    status is not None
                    and status.observed_generation >= generation
                    and status.desired_image == desired.image
                ):
                    reconciled = next(
                        (
                            condition
                            for condition in status.conditions
                            if condition.type == "Reconciled"
                            and condition.observed_generation >= generation
                        ),
                        None,
                    )
                    if reconciled is not None and reconciled.status == "True":
                        return current
                    if reconciled is not None and reconciled.status == "False":
                        raise KubernetesApplicationRejectedError(
                            reconciled.message or "controller rejected Application desired state"
                        )
            await asyncio.sleep(self._settings.observation_poll_seconds)
        raise KubernetesApplicationTransientError(
            "controller did not observe Application desired state before timeout"
        )

    async def wait_until_ready(
        self,
        desired: DesiredApplication,
        generation: int,
        lease_lost: asyncio.Event,
        timeout_seconds: float,
    ) -> ApplicationResource:
        deadline = asyncio.get_running_loop().time() + max(0, timeout_seconds)
        failure = KubernetesApplicationRolloutError(
            "DEPLOYMENT_ROLLOUT_TIMEOUT",
            "runtime did not become healthy before the deployment deadline",
            "runtime",
        )
        while asyncio.get_running_loop().time() < deadline:
            if lease_lost.is_set():
                raise KubernetesApplicationTransientError("operation lease was lost")
            try:
                current = await self.get(desired.name)
            except KubernetesApplicationTransientError:
                await asyncio.sleep(self._settings.observation_poll_seconds)
                continue
            if current is None:
                failure = KubernetesApplicationRolloutError(
                    "APPLICATION_RESOURCE_MISSING",
                    "Application resource disappeared during deployment",
                    "controller",
                )
            else:
                assessed = self._assess_readiness(current, desired, generation)
                if assessed is None:
                    return current
                failure = assessed
                if failure.code in {
                    "APPLICATION_DESIRED_STATE_REJECTED",
                    "HTTP_ROUTE_NOT_ACCEPTED",
                    "HTTP_ROUTE_REFERENCES_UNRESOLVED",
                    "CONTROLLER_RUNTIME_FAILED",
                }:
                    raise failure
                if (
                    failure.code == "CONTROLLER_HTTPS_HEALTH_FAILED"
                    and "health endpoint returned HTTP" in str(failure)
                ):
                    raise failure
            await asyncio.sleep(self._settings.observation_poll_seconds)
        raise failure

    async def verify_external_health(
        self,
        desired: DesiredApplication,
        lease_lost: asyncio.Event,
        timeout_seconds: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + max(0, timeout_seconds)
        url = f"https://{desired.name}.{self._settings.application_domain}{desired.health_path}"
        message = "external HTTPS health verification did not succeed"
        if self._health_client is None:
            raise RuntimeError("external health client is not configured")
        while asyncio.get_running_loop().time() < deadline:
            if lease_lost.is_set():
                raise KubernetesApplicationTransientError("operation lease was lost")
            try:
                response = await self._health_client.get(url)
                if response.is_success:
                    return
                message = f"external HTTPS health endpoint returned HTTP {response.status_code}"
            except (httpx.TimeoutException, httpx.TransportError):
                message = "external HTTPS health request failed"
            await asyncio.sleep(self._settings.observation_poll_seconds)
        raise KubernetesApplicationRolloutError(
            "EXTERNAL_HTTPS_HEALTH_FAILED", message, "external_https"
        )

    @staticmethod
    def _assess_readiness(
        current: ApplicationResource,
        desired: DesiredApplication,
        generation: int,
    ) -> KubernetesApplicationRolloutError | None:
        if current.metadata.deletion_timestamp is not None:
            return KubernetesApplicationRolloutError(
                "APPLICATION_DESIRED_STATE_REJECTED",
                "Application is being deleted",
                "controller",
            )
        if current.spec.release_id != desired.release_id:
            return KubernetesApplicationRolloutError(
                "APPLICATION_DEPLOYMENT_CONFLICT",
                "Application desired release changed during rollout",
                "controller",
            )
        status = current.status
        if status is None or status.observed_generation < generation:
            return KubernetesApplicationRolloutError(
                "CONTROLLER_OBSERVATION_TIMEOUT",
                "controller has not observed the requested generation",
                "controller",
            )
        conditions = {
            condition.type: condition
            for condition in status.conditions
            if condition.observed_generation >= generation
        }
        reconciled = conditions.get("Reconciled")
        if reconciled is not None and reconciled.status == "False":
            return KubernetesApplicationRolloutError(
                "APPLICATION_DESIRED_STATE_REJECTED",
                reconciled.message or "controller rejected Application desired state",
                "controller",
            )
        route = conditions.get("RouteReady")
        if route is not None and route.status == "False":
            if route.reason == "RouteNotAccepted":
                return KubernetesApplicationRolloutError(
                    "HTTP_ROUTE_NOT_ACCEPTED", route.message or "HTTPRoute was rejected", "route"
                )
            if route.reason == "RouteReferencesUnresolved":
                return KubernetesApplicationRolloutError(
                    "HTTP_ROUTE_REFERENCES_UNRESOLVED",
                    route.message or "HTTPRoute references are unresolved",
                    "route",
                )
        if status.phase == "Error":
            healthy = conditions.get("Healthy")
            if healthy is not None and healthy.reason == "ExternalHealthCheckFailed":
                return KubernetesApplicationRolloutError(
                    "CONTROLLER_HTTPS_HEALTH_FAILED",
                    healthy.message or status.message or "controller HTTPS health check failed",
                    "controller_https",
                )
            return KubernetesApplicationRolloutError(
                "CONTROLLER_RUNTIME_FAILED",
                status.message or "controller reported a terminal runtime failure",
                "runtime",
            )
        required = ("Reconciled", "RuntimeReady", "RouteReady", "Healthy", "Available")
        ready = all(
            (condition := conditions.get(name)) is not None and condition.status == "True"
            for name in required
        )
        if (
            ready
            and status.phase == "Ready"
            and status.active_release_id == desired.release_id
            and status.desired_image == desired.image
            and status.current_image == desired.image
            and status.ready_replicas == 2
        ):
            return None
        if route is None or route.status != "True":
            return KubernetesApplicationRolloutError(
                "HTTP_ROUTE_TIMEOUT",
                "waiting for accepted HTTPRoute and resolved references",
                "route",
            )
        healthy = conditions.get("Healthy")
        if healthy is not None and healthy.status == "False":
            return KubernetesApplicationRolloutError(
                "CONTROLLER_HTTPS_HEALTH_FAILED",
                healthy.message or "controller HTTPS health check failed",
                "controller_https",
            )
        return KubernetesApplicationRolloutError(
            "DEPLOYMENT_ROLLOUT_TIMEOUT",
            "waiting for desired image, two ready replicas, Service, and runtime health",
            "runtime",
        )

    @staticmethod
    def _parse(response: httpx.Response) -> ApplicationResource:
        try:
            return ApplicationResource.model_validate(response.json())
        except (ValueError, TypeError) as error:
            raise KubernetesApplicationTransientError(
                "Kubernetes API returned an invalid Application resource"
            ) from error

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code in {400, 403, 422}:
            raise KubernetesApplicationRejectedError(
                f"Kubernetes API rejected Application desired state ({response.status_code})"
            )
        raise KubernetesApplicationTransientError(
            f"Kubernetes API request failed ({response.status_code})"
        )
