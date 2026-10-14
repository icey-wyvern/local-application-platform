from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
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
from lap_platform_api.models import Application, Build, Deployment, Operation, Release
from lap_platform_api.repositories import (
    create_application,
    create_build,
    create_deployment,
    create_idempotent_operation,
    create_release,
    soft_delete_application,
)
from lap_platform_api.states import (
    BuildStatus,
    DeploymentStatus,
    DeploymentType,
    OperationStatus,
    OperationType,
)
from sqlalchemy import Engine, func, select

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[1]
TOKEN = secrets.token_urlsafe(32)


@pytest.fixture(scope="module", autouse=True)
def migrated_database(database_engine: Engine) -> None:
    with database_engine.connect() as connection:
        configuration = Config(API_ROOT / "alembic.ini")
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "head")
        connection.commit()


@pytest.fixture(scope="module")
def api_settings(tmp_path_factory: pytest.TempPathFactory) -> ApiSettings:
    temp = tmp_path_factory.mktemp("public-api")
    return ApiSettings(
        database=DatabaseSettings(
            host="postgresql",
            database="platform",
            username="platform_api",
            password="".join(("not-used", "-by-test-dependencies")),
        ),
        build_agent=BuildAgentSettings(
            url="https://build-agent.lab.test:8443",
            ca_certificate=temp / "ca.crt",
            client_certificate=temp / "client.crt",
            client_key=temp / "client.key",
        ),
        kubernetes=KubernetesSettings(enabled=False),
        scheduler=SchedulerSettings(enabled=False),
        token_sha256=hash_bearer_token(TOKEN),
    )


@pytest.fixture(scope="module")
def client(database_engine: Engine, api_settings: ApiSettings) -> TestClient:
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    dependencies = AppDependencies(
        engine=database_engine,
        session_factory=create_session_factory(database_engine),
        readiness=ReadinessChecker([], cache_seconds=5),
        build_agent=BuildAgentClient(api_settings.build_agent, client=async_client),
    )
    return TestClient(create_app(api_settings, dependencies))


def _headers(key: UUID | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if key is not None:
        headers["Idempotency-Key"] = str(key)
    return headers


def _deployment_body(repository: str) -> dict[str, object]:
    return {
        "repositoryUrl": repository,
        "revision": {"type": "branch", "value": "main"},
    }


def _seed_history(database_engine: Engine, suffix: str) -> tuple[str, UUID, UUID, UUID, UUID]:
    factory = create_session_factory(database_engine)
    name = f"history-{suffix}"
    repository = f"https://example.com/{name}.git"
    with factory.begin() as session:
        application = create_application(
            session,
            name=name,
            repository_url=repository,
            normalized_repository_url=repository,
        )
        operation, _ = create_idempotent_operation(
            session,
            operation_type=OperationType.DEPLOY,
            idempotency_key=uuid4(),
            request_hash="a" * 64,
            correlation_id=uuid4(),
            repository_url=repository,
            normalized_repository_url=repository,
            git_revision="refs/heads/main",
            application_id=application.id,
        )
        build = create_build(
            session,
            operation=operation,
            application=application,
            repository_url=repository,
            normalized_repository_url=repository,
            requested_revision="refs/heads/main",
        )
        build.status = BuildStatus.SUCCEEDED.value
        build.resolved_commit = "b" * 40
        build.completed_at = datetime.now(UTC)
        session.flush()
        digest = "sha256:" + "c" * 64
        release = create_release(
            session,
            build=build,
            application=application,
            image_reference=f"registry.lab.test:5000/apps/{name}@{digest}",
            image_digest=digest,
            port=8080,
            health_path="/healthz",
        )
        deployment = create_deployment(
            session,
            operation=operation,
            application=application,
            desired_release=release,
            deployment_type=DeploymentType.RELEASE,
        )
        deployment.status = DeploymentStatus.HEALTHY.value
        deployment.completed_at = datetime.now(UTC)
        operation.status = OperationStatus.SUCCEEDED.value
        operation.completed_at = datetime.now(UTC)
        session.flush()
        return name, operation.id, build.id, release.id, deployment.id


def test_routes_require_authentication(client: TestClient) -> None:
    for method, path in (
        ("get", "/api/v1/applications"),
        ("post", "/api/v1/deployments"),
        ("get", f"/api/v1/operations/{uuid4()}"),
    ):
        response = client.request(method, path)
        assert response.status_code == 401
        assert response.json()["errorCode"] == "AUTHENTICATION_FAILED"


def test_submit_creates_durable_build_and_replays_idempotently(
    client: TestClient, database_engine: Engine
) -> None:
    key = uuid4()
    repository = f"https://EXAMPLE.com/{uuid4()}/"
    body = _deployment_body(repository)

    first = client.post("/api/v1/deployments", headers=_headers(key), json=body)
    replay = client.post("/api/v1/deployments", headers=_headers(key), json=body)

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    operation_id = UUID(first.json()["id"])
    assert first.headers["location"] == f"/api/v1/operations/{operation_id}"
    assert first.json()["status"] == "queued"
    assert first.json()["buildId"] is not None

    factory = create_session_factory(database_engine)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Operation).where(Operation.idempotency_key == key)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Build).where(Build.operation_id == operation_id)
            )
            == 1
        )


def test_idempotency_conflict_and_validation_errors(client: TestClient) -> None:
    key = uuid4()
    first = client.post(
        "/api/v1/deployments",
        headers=_headers(key),
        json=_deployment_body(f"https://example.com/{uuid4()}.git"),
    )
    conflict = client.post(
        "/api/v1/deployments",
        headers=_headers(key),
        json=_deployment_body(f"https://example.com/{uuid4()}.git"),
    )
    missing_key = client.post(
        "/api/v1/deployments",
        headers=_headers(),
        json=_deployment_body("https://example.com/missing-key.git"),
    )
    invalid_url = client.post(
        "/api/v1/deployments",
        headers=_headers(uuid4()),
        json=_deployment_body("http://example.com/repository.git"),
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["errorCode"] == "IDEMPOTENCY_CONFLICT"
    assert missing_key.status_code == 400
    assert missing_key.json()["errorCode"] == "IDEMPOTENCY_KEY_INVALID"
    assert invalid_url.status_code == 422
    assert invalid_url.json()["errorCode"] == "REPOSITORY_URL_INVALID"
    for response in (conflict, missing_key, invalid_url):
        assert response.headers["content-type"].startswith("application/problem+json")


def test_concurrent_duplicate_submission_creates_one_operation_and_build(
    client: TestClient, database_engine: Engine
) -> None:
    key = uuid4()
    body = _deployment_body(f"https://example.com/{uuid4()}.git")

    def submit() -> httpx.Response:
        return client.post("/api/v1/deployments", headers=_headers(key), json=body)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit(), range(2)))

    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    operation_id = UUID(responses[0].json()["id"])
    factory = create_session_factory(database_engine)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Operation).where(Operation.idempotency_key == key)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Build).where(Build.operation_id == operation_id)
            )
            == 1
        )


def test_application_and_complete_history_reads(
    client: TestClient, database_engine: Engine
) -> None:
    suffix = uuid4().hex[:10]
    name, operation_id, build_id, release_id, deployment_id = _seed_history(database_engine, suffix)

    listing = client.get("/api/v1/applications", headers=_headers())
    application = client.get(f"/api/v1/applications/{name}", headers=_headers())
    builds = client.get(f"/api/v1/applications/{name}/builds", headers=_headers())
    releases = client.get(f"/api/v1/applications/{name}/releases", headers=_headers())
    deployments = client.get(f"/api/v1/applications/{name}/deployments", headers=_headers())
    operations = client.get(f"/api/v1/applications/{name}/operations", headers=_headers())

    assert listing.status_code == application.status_code == 200
    assert name in {item["name"] for item in listing.json()}
    assert application.json()["name"] == name
    assert builds.json()[0]["id"] == str(build_id)
    assert releases.json()[0]["id"] == str(release_id)
    assert deployments.json()[0]["id"] == str(deployment_id)
    assert operations.json()[0]["id"] == str(operation_id)
    assert client.get(f"/api/v1/builds/{build_id}", headers=_headers()).status_code == 200
    assert client.get(f"/api/v1/releases/{release_id}", headers=_headers()).status_code == 200
    assert client.get(f"/api/v1/deployments/{deployment_id}", headers=_headers()).status_code == 200
    assert client.get(f"/api/v1/operations/{operation_id}", headers=_headers()).status_code == 200


def test_delete_records_intent_and_soft_deleted_identity_is_hidden_but_preserved(
    client: TestClient, database_engine: Engine
) -> None:
    suffix = uuid4().hex[:10]
    name, historical_operation_id, _, _, _ = _seed_history(database_engine, suffix)
    key = uuid4()

    accepted = client.delete(f"/api/v1/applications/{name}", headers=_headers(key))
    replay = client.delete(f"/api/v1/applications/{name}", headers=_headers(key))
    assert accepted.status_code == replay.status_code == 202
    assert accepted.json() == replay.json()
    assert accepted.json()["type"] == "delete"

    factory = create_session_factory(database_engine)
    with factory.begin() as session:
        application_id = session.scalar(
            select(Operation.application_id).where(Operation.id == UUID(accepted.json()["id"]))
        )
        assert application_id is not None
        stored_application = session.get(Application, application_id)
        assert stored_application is not None
        soft_delete_application(session, stored_application)

    assert client.get(f"/api/v1/applications/{name}", headers=_headers()).status_code == 404
    listing = client.get("/api/v1/applications", headers=_headers())
    assert name not in {item["name"] for item in listing.json()}
    historical = client.get(f"/api/v1/operations/{historical_operation_id}", headers=_headers())
    deletion = client.get(f"/api/v1/operations/{accepted.json()['id']}", headers=_headers())
    assert historical.status_code == deletion.status_code == 200
    replay_after_deletion = client.delete(f"/api/v1/applications/{name}", headers=_headers(key))
    assert replay_after_deletion.status_code == 202
    assert replay_after_deletion.json() == accepted.json()


def test_manual_rollback_records_only_a_previously_healthy_release(
    client: TestClient, database_engine: Engine
) -> None:
    name, _, _, earlier_id, _ = _seed_history(database_engine, uuid4().hex[:10])
    factory = create_session_factory(database_engine)
    with factory.begin() as session:
        application = session.scalar(select(Application).where(Application.name == name))
        assert application is not None
        application.current_release_id = earlier_id
        operation, _ = create_idempotent_operation(
            session,
            operation_type=OperationType.DEPLOY,
            idempotency_key=uuid4(),
            request_hash="b" * 64,
            correlation_id=uuid4(),
            application_id=application.id,
        )
        build = create_build(
            session,
            operation=operation,
            application=application,
            repository_url=application.repository_url,
            normalized_repository_url=application.normalized_repository_url,
            requested_revision="refs/heads/main",
        )
        build.status = BuildStatus.SUCCEEDED.value
        build.resolved_commit = "d" * 40
        build.completed_at = datetime.now(UTC)
        session.flush()
        digest = "sha256:" + "e" * 64
        newer = create_release(
            session,
            build=build,
            application=application,
            image_reference=f"registry.lab.test:5000/apps/{name}@{digest}",
            image_digest=digest,
            port=8080,
            health_path="/healthz",
        )
        deployed = create_deployment(
            session,
            operation=operation,
            application=application,
            desired_release=newer,
            previous_release=session.get(Release, earlier_id),
            deployment_type=DeploymentType.RELEASE,
        )
        deployed.status = DeploymentStatus.HEALTHY.value
        deployed.completed_at = datetime.now(UTC)
        operation.status = OperationStatus.SUCCEEDED.value
        operation.completed_at = datetime.now(UTC)
        application.current_release_id = newer.id
        newer_id = newer.id
    url = f"/api/v1/applications/{name}/rollbacks"
    key = uuid4()
    body = {"releaseId": str(earlier_id)}
    accepted = client.post(url, headers=_headers(key), json=body)
    replay = client.post(url, headers=_headers(key), json=body)
    assert accepted.status_code == replay.status_code == 202
    assert accepted.json() == replay.json()
    assert accepted.json()["type"] == "rollback"
    assert accepted.json()["buildId"] is None
    assert accepted.json()["deploymentId"] is not None
    with factory() as session:
        intent = session.get(Deployment, UUID(accepted.json()["deploymentId"]))
        assert intent is not None
        assert intent.type == DeploymentType.MANUAL_ROLLBACK.value
        assert intent.desired_release_id == earlier_id
        assert intent.previous_release_id == newer_id
        assert intent.status == DeploymentStatus.PENDING.value
    for rejected_id in (newer_id, uuid4()):
        rejected = client.post(url, headers=_headers(uuid4()), json={"releaseId": str(rejected_id)})
        assert rejected.status_code == 409
        assert rejected.json()["errorCode"] == "ROLLBACK_TARGET_INVALID"
    conflict = client.post(url, headers=_headers(key), json={"releaseId": str(newer_id)})
    assert conflict.status_code == 409
    assert conflict.json()["errorCode"] == "IDEMPOTENCY_CONFLICT"
