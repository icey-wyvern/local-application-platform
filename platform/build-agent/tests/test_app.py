from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from lap_build_agent.app import create_app
from lap_build_agent.config import Settings
from lap_build_agent.errors import BuildError
from lap_build_agent.models import (
    ApplicationMetadata,
    BuildRequest,
    BuildResult,
    VulnerabilitySummary,
)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        listen_host="127.0.0.1",
        listen_port=8443,
        state_path=tmp_path,
        buildkit_socket=tmp_path / "buildkit.sock",
        trivy_cache_path=tmp_path / "cache",
        registry="registry.lab.test:5000",
        registry_namespace="apps",
        registry_auth_file=tmp_path / "auth.json",
        vulnerability_threshold="CRITICAL",
        build_timeout_seconds=900,
        otlp_endpoint=None,
    )


class SuccessfulPipeline:
    def execute(self, request: BuildRequest) -> BuildResult:
        digest = "sha256:" + "a" * 64
        return BuildResult(
            buildId=request.buildId,
            commit="b" * 40,
            imageReference=f"registry.lab.test:5000/apps/example@{digest}",
            imageDigest=digest,
            application=ApplicationMetadata(name="example", port=8080, healthPath="/health"),
            vulnerabilities=VulnerabilitySummary(
                threshold="CRITICAL",
                counts={"UNKNOWN": 0, "LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0},
                blocked=0,
            ),
        )


class FailingPipeline:
    def execute(self, _request: BuildRequest) -> BuildResult:
        raise BuildError("build_failed", "buildctl exited with status 1", stage="build")


REQUEST = {
    "buildId": str(UUID("00000000-0000-4000-8000-000000000001")),
    "repositoryUrl": "https://example.com/repo.git",
    "revision": {"type": "branch", "value": "main"},
}


def test_build_result_contract(tmp_path: Path) -> None:
    client = TestClient(create_app(settings(tmp_path), lambda: SuccessfulPipeline()))
    response = client.post("/v1/builds", json=REQUEST)
    assert response.status_code == 200
    assert response.json()["commit"] == "b" * 40
    assert response.json()["imageDigest"] == "sha256:" + "a" * 64


def test_machine_readable_build_failure(tmp_path: Path) -> None:
    client = TestClient(create_app(settings(tmp_path), lambda: FailingPipeline()))
    response = client.post("/v1/builds", json=REQUEST)
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "build_failed",
            "message": "buildctl exited with status 1",
            "stage": "build",
            "retryable": False,
        }
    }


def test_unknown_request_properties_fail(tmp_path: Path) -> None:
    client = TestClient(create_app(settings(tmp_path), lambda: SuccessfulPipeline()))
    response = client.post("/v1/builds", json={**REQUEST, "credentials": "forbidden"})
    assert response.status_code == 422


def test_duplicate_build_id_returns_the_existing_result(tmp_path: Path) -> None:
    calls = 0

    class CountingPipeline(SuccessfulPipeline):
        def execute(self, request: BuildRequest) -> BuildResult:
            nonlocal calls
            calls += 1
            return super().execute(request)

    client = TestClient(create_app(settings(tmp_path), lambda: CountingPipeline()))
    assert client.post("/v1/builds", json=REQUEST).status_code == 200
    assert client.post("/v1/builds", json=REQUEST).status_code == 200
    assert calls == 1


def test_reused_build_id_with_different_request_conflicts(tmp_path: Path) -> None:
    client = TestClient(create_app(settings(tmp_path), lambda: SuccessfulPipeline()))
    assert client.post("/v1/builds", json=REQUEST).status_code == 200
    changed = {**REQUEST, "revision": {"type": "tag", "value": "v1"}}
    response = client.post("/v1/builds", json=changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "build_id_conflict"


def test_retryable_failure_reuses_build_identity_and_can_recover(tmp_path: Path) -> None:
    calls = 0

    class RecoveringPipeline(SuccessfulPipeline):
        def execute(self, request: BuildRequest) -> BuildResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BuildError("clone_failed", "source temporarily unavailable", retryable=True)
            return super().execute(request)

    client = TestClient(create_app(settings(tmp_path), lambda: RecoveringPipeline()))
    first = client.post("/v1/builds", json=REQUEST)
    assert first.status_code == 422
    assert first.json()["error"]["retryable"] is True
    changed = {**REQUEST, "revision": {"type": "tag", "value": "v1"}}
    assert client.post("/v1/builds", json=changed).status_code == 409
    assert client.post("/v1/builds", json=REQUEST).status_code == 200
    assert client.post("/v1/builds", json=REQUEST).status_code == 200
    assert calls == 2
