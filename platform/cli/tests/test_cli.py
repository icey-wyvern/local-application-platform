from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from lap_cli import main
from typer.testing import CliRunner

TOKEN = "test-" + "secret-token"
RUNNER = CliRunner()


def _operation(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "type": "deploy",
        "status": status,
        "buildId": str(uuid4()),
        "failureCode": None,
        "failureMessage": None,
        "failureResult": None,
        **extra,
    }


def _mock(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    original = main.Client

    def factory(base: str, token: str, verify: str | bool) -> main.Client:
        client = original(base, token, verify)

        def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)

        client.http.close()
        client.http = httpx.Client(
            base_url=base,
            headers={"Authorization": f"Bearer {token}"},
            transport=httpx.MockTransport(record),
        )
        return client

    monkeypatch.setattr(main, "Client", factory)
    monkeypatch.setenv("LAP_API_TOKEN", TOKEN)
    return requests


def test_config_and_revision_parsing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LAP_API_TOKEN", raising=False)
    credentials = tmp_path / "credentials"
    credentials.write_text(TOKEN)
    credentials.chmod(0o600)
    monkeypatch.setenv("LAP_CREDENTIALS_FILE", str(credentials))
    assert main._configuration(None, None) == ("https://api.lab.test", TOKEN, True)
    assert main._revision("refs/tags/v1", None) == {"type": "tag", "value": "v1"}
    assert main._revision("a" * 40, None) == {"type": "commit", "value": "a" * 40}
    assert main._revision("main", None) == {"type": "branch", "value": "main"}
    assert RUNNER.invoke(main.app, ["deploy", "repo"]).exit_code != 0
    monkeypatch.setenv("LAP_API_URL", "http://api.lab.test")
    with pytest.raises(main.CliError, match="HTTPS origin"):
        main._configuration(None, None)
    monkeypatch.setenv("LAP_API_URL", "https://api.lab.test:invalid")
    with pytest.raises(main.CliError, match="API URL is invalid"):
        main._configuration(None, None)


def test_deploy_auth_idempotency_poll_and_result(monkeypatch: pytest.MonkeyPatch) -> None:
    queued = _operation("queued")
    running = {**queued, "status": "running"}
    complete = {**queued, "status": "succeeded"}
    polls = iter((running, complete))
    release_id = str(uuid4())
    app_id = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/deployments"):
            assert json.loads(request.content) == {
                "repositoryUrl": "https://example.com/app.git",
                "revision": {"type": "branch", "value": "main"},
            }
            return httpx.Response(202, json=queued)
        if "/operations/" in path:
            return httpx.Response(200, json=next(polls))
        if "/builds/" in path:
            return httpx.Response(200, json={"releaseId": release_id})
        if "/releases/" in path:
            return httpx.Response(
                200,
                json={
                    "id": release_id,
                    "applicationId": app_id,
                    "imageReference": "registry/image@sha256:abc",
                },
            )
        return httpx.Response(
            200, json=[{"id": app_id, "name": "app", "currentReleaseId": release_id}]
        )

    requests = _mock(monkeypatch, handler)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    result = RUNNER.invoke(
        main.app,
        ["--poll-interval", "0.1", "deploy", "https://example.com/app.git", "--revision", "main"],
    )
    assert result.exit_code == 0, result.output
    assert "Operation" in result.output and "running" in result.output
    assert "https://app.apps.lab.test" in result.output
    assert all(request.headers["Authorization"] == f"Bearer {TOKEN}" for request in requests)
    assert UUID(requests[0].headers["Idempotency-Key"]).version == 4
    assert UUID(requests[0].headers["X-Request-ID"]).version == 4
    assert TOKEN not in result.output


def test_problem_failure_and_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = _operation(
        "failed",
        failureCode="DEPLOYMENT_UNHEALTHY",
        failureMessage="not ready",
        failureResult={"rollbackStatus": "succeeded", "restoredReleaseId": "old"},
    )
    _mock(monkeypatch, lambda _: httpx.Response(202, json=failed))
    result = RUNNER.invoke(
        main.app, ["deploy", "https://example.com/app.git", "--revision", "main"]
    )
    assert result.exit_code == main.EXIT_API
    assert "DEPLOYMENT_UNHEALTHY: not ready" in result.output
    assert "Automatic rollback: succeeded" in result.output
    assert "Restored release: old" in result.output

    _mock(
        monkeypatch,
        lambda _: httpx.Response(
            403,
            headers={"content-type": "application/problem+json"},
            json={"errorCode": "FORBIDDEN", "detail": f"denied {TOKEN}"},
        ),
    )
    rejected = RUNNER.invoke(main.app, ["list"])
    assert rejected.exit_code == main.EXIT_API
    assert "FORBIDDEN" in rejected.output
    assert TOKEN not in rejected.output


def test_manual_rollback_polls_and_checks_selected_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = str(uuid4())
    queued = {**_operation("queued"), "type": "rollback", "buildId": None}
    complete = {**queued, "status": "succeeded"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/v1/applications/app/rollbacks"
            assert json.loads(request.content) == {"releaseId": selected}
            return httpx.Response(202, json=queued)
        if "/operations/" in request.url.path:
            return httpx.Response(200, json=complete)
        return httpx.Response(200, json={"name": "app", "currentReleaseId": selected})

    requests = _mock(monkeypatch, handler)
    result = RUNNER.invoke(main.app, ["rollback", "app", "--release", selected])
    assert result.exit_code == 0, result.output
    assert f"Restored Release: {selected}" in result.output
    assert UUID(requests[0].headers["Idempotency-Key"]).version == 4


def test_status_list_history_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = str(uuid4())
    release_id = str(uuid4())
    item = {"id": app_id, "name": "app", "currentReleaseId": release_id}
    deployment = {
        "id": str(uuid4()),
        "createdAt": "2026-01-01T00:00:02Z",
        "type": "automatic_rollback",
        "status": "healthy",
        "desiredReleaseId": release_id,
    }
    operation = {
        **_operation("failed", failureResult={"rollbackStatus": "succeeded"}),
        "createdAt": "2026-01-01T00:00:01Z",
        "failureCode": "UNHEALTHY",
        "failureMessage": "failed",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "DELETE":
            return httpx.Response(202, json=_operation("succeeded", type="delete"))
        if path.endswith("/applications"):
            return httpx.Response(200, json=[item])
        if path.endswith("/app"):
            return httpx.Response(200, json=item)
        if path.endswith("/deployments"):
            return httpx.Response(200, json=[deployment])
        if path.endswith("/operations"):
            return httpx.Response(200, json=[operation])
        if path.endswith("/builds"):
            return httpx.Response(200, json=[])
        if path.endswith("/releases"):
            return httpx.Response(
                200, json=[{"id": release_id, "createdAt": "2026-01-01T00:00:00Z"}]
            )
        return httpx.Response(
            200, json={"id": release_id, "imageReference": "registry/image@sha256:abc"}
        )

    requests = _mock(monkeypatch, handler)
    assert "app" in RUNNER.invoke(main.app, ["list"]).output
    assert "automatic_rollback healthy" in RUNNER.invoke(main.app, ["status", "app"]).output
    history = RUNNER.invoke(main.app, ["history", "app"])
    assert history.exit_code == 0
    assert "automatic rollback: succeeded" in history.output
    assert "restores" in history.output
    deleted = RUNNER.invoke(main.app, ["delete", "app"])
    assert deleted.exit_code == 0
    assert "Delete: succeeded" in deleted.output
    assert UUID(requests[-1].headers["Idempotency-Key"]).version == 4
    assert json.loads(RUNNER.invoke(main.app, ["--json", "list"]).output)[0]["name"] == "app"


def test_timeout_and_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(
        monkeypatch,
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("secret", request=request)),
    )
    network = RUNNER.invoke(main.app, ["list"])
    assert network.exit_code == main.EXIT_RUNTIME
    assert "Cannot connect" in network.output and "secret" not in network.output
    _mock(
        monkeypatch,
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("secret", request=request)),
    )
    timeout = RUNNER.invoke(main.app, ["list"])
    assert timeout.exit_code == main.EXIT_RUNTIME
    assert "timed out" in timeout.output and "secret" not in timeout.output


def test_poll_deadline_and_json_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    queued = _operation("queued")
    _mock(monkeypatch, lambda _: httpx.Response(202, json=queued))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    times = iter((0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(times))
    expired = RUNNER.invoke(
        main.app,
        ["--poll-timeout", "1", "deploy", "https://example.com/app.git", "--revision", "main"],
    )
    assert expired.exit_code == main.EXIT_RUNTIME
    assert "did not complete" in expired.output

    _mock(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json=[{"id": str(uuid4()), "name": "app", "repositoryUrl": TOKEN}],
        ),
    )
    redacted = RUNNER.invoke(main.app, ["--json", "list"])
    assert redacted.exit_code == 0
    assert TOKEN not in redacted.output
    assert json.loads(redacted.output)[0]["repositoryUrl"] == "<redacted>"


def test_ctrl_c_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(monkeypatch, lambda _: httpx.Response(202, json=_operation("queued")))

    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupt)
    result = RUNNER.invoke(
        main.app, ["deploy", "https://example.com/app.git", "--revision", "main"]
    )
    assert result.exit_code == 130
    assert "Interrupted" in result.output
