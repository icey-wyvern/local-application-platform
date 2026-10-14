"""HTTP-only operator client for the platform API."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import httpx
import typer

app = typer.Typer(no_args_is_help=True, help="Manage applications through the platform API.")
API_PREFIX = "/api/v1"
TERMINAL = {"succeeded", "failed"}
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
EXIT_API = 2
EXIT_RUNTIME = 3


class CliError(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _command[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except CliError as error:
            typer.echo(f"Error: {error}", err=True)
            raise typer.Exit(error.exit_code) from None
        except KeyboardInterrupt:
            typer.echo("Interrupted", err=True)
            raise typer.Exit(130) from None

    return wrapped


def _redact(value: str, token: str) -> str:
    return value.replace(token, "<redacted>") if token else value


def _sanitize(value: Any, token: str) -> Any:
    if isinstance(value, str):
        return _redact(value, token)
    if isinstance(value, list):
        return [_sanitize(item, token) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize(item, token) for key, item in value.items()}
    return value


def _configuration(url: str | None, ca_bundle: Path | None) -> tuple[str, str, str | bool]:
    base = url or os.environ.get("LAP_API_URL") or "https://api.lab.test"
    try:
        parsed = urlsplit(base)
        port = parsed.port
    except ValueError as error:
        raise CliError("API URL is invalid", EXIT_RUNTIME) from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port == 0
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise CliError(
            "API URL must be an HTTPS origin without credentials or a path", EXIT_RUNTIME
        )
    credential = os.environ.get("LAP_API_TOKEN")
    if credential is None:
        path = Path(
            os.environ.get(
                "LAP_CREDENTIALS_FILE",
                str(Path.home() / ".config/local-application-platform/credentials"),
            )
        )
        try:
            if os.name != "nt" and path.stat().st_mode & 0o077:
                raise CliError(f"Credentials file permissions must be 0600: {path}", EXIT_RUNTIME)
            credential = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as error:
            raise CliError(
                "Missing bearer token; set LAP_API_TOKEN or create the credentials file",
                EXIT_RUNTIME,
            ) from error
        except OSError as error:
            raise CliError("Cannot read credentials file", EXIT_RUNTIME) from error
    if (
        credential is None
        or not credential
        or credential != credential.strip()
        or any(c.isspace() for c in credential)
    ):
        raise CliError("Bearer token is empty or malformed", EXIT_RUNTIME)
    bundle = ca_bundle or os.environ.get("LAP_CA_BUNDLE")
    if bundle and not Path(bundle).is_file():
        raise CliError("CA bundle does not exist", EXIT_RUNTIME)
    return base.rstrip("/"), credential, str(bundle) if bundle else True


class Client:
    def __init__(self, base: str, token: str, verify: str | bool) -> None:
        self.token = token
        self.http = httpx.Client(
            base_url=base,
            headers={"Authorization": f"Bearer {token}"},
            verify=verify,
            timeout=httpx.Timeout(20, connect=5),
            follow_redirects=False,
        )

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_args: object) -> None:
        self.http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        mutate: bool = False,
        params: dict[str, int] | None = None,
    ) -> Any:
        headers = {"X-Request-ID": str(uuid4())}
        if mutate:
            headers["Idempotency-Key"] = str(uuid4())
        try:
            response = self.http.request(method, path, json=body, headers=headers, params=params)
        except httpx.TimeoutException as error:
            raise CliError("API request timed out", EXIT_RUNTIME) from error
        except httpx.RequestError as error:
            raise CliError(
                "Cannot connect to the platform API (check DNS, TLS and network)", EXIT_RUNTIME
            ) from error
        if response.is_error:
            if response.headers.get("content-type", "").startswith("application/problem+json"):
                try:
                    problem = response.json()
                    code = problem.get("errorCode", "API_ERROR")
                    detail = problem.get("detail", "request rejected")
                    raise CliError(_redact(f"{code}: {detail}", self.token), EXIT_API)
                except (ValueError, AttributeError) as error:
                    raise CliError(
                        f"API returned invalid problem response ({response.status_code})", EXIT_API
                    ) from error
            raise CliError(f"API returned HTTP {response.status_code}", EXIT_API)
        try:
            return _sanitize(response.json(), self.token)
        except ValueError as error:
            raise CliError("API returned invalid JSON", EXIT_RUNTIME) from error

    def collection(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            page = self.request("GET", path, params={"offset": len(items), "limit": 100})
            if not isinstance(page, list):
                raise CliError("API returned an invalid collection", EXIT_RUNTIME)
            items.extend(page)
            if len(page) < 100:
                return items


def _name_path(name: str) -> str:
    if not NAME.fullmatch(name):
        raise CliError("Application name is invalid", EXIT_RUNTIME)
    return f"{API_PREFIX}/applications/{quote(name)}"


def _emit(value: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, indent=2))
    elif isinstance(value, str):
        typer.echo(value)


def _poll(
    client: Client, operation: dict[str, Any], *, interval: float, timeout: float, json_output: bool
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    previous = ""
    while True:
        state = str(operation.get("status", "unknown"))
        if state != previous and not json_output:
            typer.echo(f"Operation {operation['id']}: {state}")
        previous = state
        if state in TERMINAL:
            return operation
        if time.monotonic() >= deadline:
            raise CliError(
                f"Operation {operation['id']} did not complete within {timeout:g}s", EXIT_RUNTIME
            )
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
        operation = client.request("GET", f"{API_PREFIX}/operations/{UUID(operation['id'])}")


def _failure(operation: dict[str, Any]) -> str:
    code = operation.get("failureCode") or "OPERATION_FAILED"
    message = operation.get("failureMessage") or "operation failed"
    lines = [f"{code}: {message}"]
    result = operation.get("failureResult") or {}
    if "rollbackStatus" in result:
        lines.append(f"Automatic rollback: {result['rollbackStatus']}")
        if result.get("restoredReleaseId"):
            lines.append(f"Restored release: {result['restoredReleaseId']}")
        if result.get("rollbackDeploymentId"):
            lines.append(f"Rollback deployment: {result['rollbackDeploymentId']}")
    return "\n".join(lines)


def _revision(value: str, kind: str | None) -> dict[str, str]:
    if kind is None:
        if value.startswith("refs/tags/"):
            kind, value = "tag", value.removeprefix("refs/tags/")
        elif value.startswith("refs/heads/"):
            kind, value = "branch", value.removeprefix("refs/heads/")
        elif re.fullmatch(r"[0-9a-fA-F]{40}", value):
            kind = "commit"
        else:
            kind = "branch"
    if kind not in {"branch", "tag", "commit"} or not value:
        raise CliError("Revision must be a branch, tag or commit", EXIT_RUNTIME)
    return {"type": kind, "value": value}


def _context(ctx: typer.Context) -> tuple[Client, bool, float, float]:
    settings = ctx.obj
    base, token, verify = _configuration(settings["url"], settings["ca_bundle"])
    return (
        Client(base, token, verify),
        settings["json"],
        settings["poll_interval"],
        settings["poll_timeout"],
    )


@app.callback()
def options(
    ctx: typer.Context,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API HTTPS origin")] = None,
    ca_bundle: Annotated[
        Path | None, typer.Option("--ca-bundle", help="Trusted lab root CA PEM")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
    poll_interval: Annotated[float, typer.Option("--poll-interval", min=0.1, max=60)] = 2,
    poll_timeout: Annotated[float, typer.Option("--poll-timeout", min=1, max=3600)] = 1200,
) -> None:
    ctx.obj = {
        "url": api_url,
        "ca_bundle": ca_bundle,
        "json": json_output,
        "poll_interval": poll_interval,
        "poll_timeout": poll_timeout,
    }


@app.command()
@_command
def deploy(
    ctx: typer.Context,
    repository: str,
    revision: Annotated[
        str, typer.Option("--revision", help="Branch, refs/tags/name, or full commit SHA")
    ],
    revision_type: Annotated[
        str | None, typer.Option("--revision-type", help="branch, tag, or commit")
    ] = None,
) -> None:
    client, machine, interval, timeout = _context(ctx)
    with client:
        operation = client.request(
            "POST",
            f"{API_PREFIX}/deployments",
            body={"repositoryUrl": repository, "revision": _revision(revision, revision_type)},
            mutate=True,
        )
        if not machine:
            typer.echo(f"Submitted deployment: {operation['id']}")
        operation = _poll(
            client, operation, interval=interval, timeout=timeout, json_output=machine
        )
        if operation["status"] == "failed":
            _emit(operation if machine else _failure(operation), json_output=machine)
            raise typer.Exit(EXIT_API)
        build = client.request("GET", f"{API_PREFIX}/builds/{operation['buildId']}")
        release = client.request("GET", f"{API_PREFIX}/releases/{build['releaseId']}")
        applications = client.collection(f"{API_PREFIX}/applications")
        application = next(
            (item for item in applications if item["id"] == release["applicationId"]), None
        )
        if application is None:
            raise CliError("Deployment completed but application lookup failed", EXIT_RUNTIME)
        result = {
            "operation": operation,
            "application": application,
            "release": release,
            "url": f"https://{application['name']}.apps.lab.test",
        }
        summary = (
            f"Application: {application['name']}\n"
            f"Release: {release['id']}\n"
            f"Image: {release['imageReference']}\n"
            f"URL: {result['url']}"
        )
        _emit(result if machine else summary, json_output=machine)


@app.command()
@_command
def rollback(
    ctx: typer.Context,
    application: str,
    release: Annotated[UUID, typer.Option("--release", help="Previously healthy Release UUID")],
) -> None:
    client, machine, interval, timeout = _context(ctx)
    path = _name_path(application)
    with client:
        operation = client.request(
            "POST",
            f"{path}/rollbacks",
            body={"releaseId": str(release)},
            mutate=True,
        )
        if not machine:
            typer.echo(f"Submitted historical Release activation: {operation['id']}")
        operation = _poll(
            client, operation, interval=interval, timeout=timeout, json_output=machine
        )
        if operation["status"] == "failed":
            _emit(operation if machine else _failure(operation), json_output=machine)
            raise typer.Exit(EXIT_API)
        current = client.request("GET", path)
        if current.get("currentReleaseId") != str(release):
            raise CliError(
                "Rollback completed without activating the selected Release", EXIT_RUNTIME
            )
        result = {"operation": operation, "application": current, "releaseId": str(release)}
        _emit(
            result if machine else f"Application: {application}\nRestored Release: {release}",
            json_output=machine,
        )


@app.command("list")
@_command
def list_applications(ctx: typer.Context) -> None:
    client, machine, _, _ = _context(ctx)
    with client:
        applications = client.collection(f"{API_PREFIX}/applications")
        _emit(
            applications
            if machine
            else "\n".join(
                f"{item['name']}  {item.get('currentReleaseId') or 'no active release'}"
                for item in applications
            )
            or "No active applications",
            json_output=machine,
        )


@app.command()
@_command
def status(ctx: typer.Context, application: str) -> None:
    client, machine, _, _ = _context(ctx)
    path = _name_path(application)
    with client:
        app_data = client.request("GET", path)
        deployments = client.collection(f"{path}/deployments")
        operations = client.collection(f"{path}/operations")
        release = (
            client.request("GET", f"{API_PREFIX}/releases/{app_data['currentReleaseId']}")
            if app_data.get("currentReleaseId")
            else None
        )
        latest_deployment = max(deployments, key=lambda item: item["createdAt"], default=None)
        latest_operation = max(operations, key=lambda item: item["createdAt"], default=None)
        result = {
            "application": app_data,
            "release": release,
            "latestDeployment": latest_deployment,
            "latestOperation": latest_operation,
            "url": f"https://{application}.apps.lab.test",
        }
        lines = [
            f"Application: {application}",
            f"Runtime: {latest_deployment['status'] if latest_deployment else 'no deployment'}",
            f"Release: {release['id'] if release else 'none'}",
        ]
        if release:
            lines.extend((f"Image: {release['imageReference']}", f"URL: {result['url']}"))
        if latest_deployment:
            lines.append(
                f"Latest deployment: {latest_deployment['type']} {latest_deployment['status']}"
            )
        if latest_operation:
            lines.append(
                f"Latest operation: {latest_operation['type']} {latest_operation['status']}"
            )
            if latest_operation["status"] == "failed":
                lines.append(_failure(latest_operation))
        _emit(result if machine else "\n".join(lines), json_output=machine)


@app.command()
@_command
def history(ctx: typer.Context, application: str) -> None:
    client, machine, _, _ = _context(ctx)
    path = _name_path(application)
    with client:
        client.request("GET", path)
        groups = {
            kind: client.collection(f"{path}/{kind}")
            for kind in ("builds", "releases", "deployments", "operations")
        }
        if machine:
            _emit(groups, json_output=True)
            return
        entries: list[tuple[str, str]] = []
        for kind, records in groups.items():
            for item in records:
                line = (
                    f"{item['createdAt']}  {kind[:-1]:10} {item['id']}  "
                    f"{item.get('type', '')} {item.get('status', '')}"
                ).rstrip()
                if item.get("failureCode"):
                    line += f"  {item['failureCode']}: {item.get('failureMessage') or ''}"
                if kind == "deployments" and item.get("type") == "automatic_rollback":
                    line += f"  restores {item['desiredReleaseId']}"
                if kind == "operations" and (item.get("failureResult") or {}).get("rollbackStatus"):
                    line += f"  automatic rollback: {item['failureResult']['rollbackStatus']}"
                entries.append((item["createdAt"], line))
        _emit("\n".join(line for _, line in sorted(entries)) or "No history", json_output=False)


@app.command()
@_command
def delete(
    ctx: typer.Context,
    application: str,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Poll until terminal")] = True,
) -> None:
    client, machine, interval, timeout = _context(ctx)
    with client:
        operation = client.request("DELETE", _name_path(application), mutate=True)
        if not machine:
            typer.echo(f"Submitted deletion: {operation['id']}")
        if wait:
            operation = _poll(
                client, operation, interval=interval, timeout=timeout, json_output=machine
            )
        _emit(
            operation
            if machine
            else (
                _failure(operation)
                if operation["status"] == "failed"
                else f"Delete: {operation['status']}"
            ),
            json_output=machine,
        )
        if operation["status"] == "failed":
            raise typer.Exit(EXIT_API)


@app.command()
@_command
def operation(
    ctx: typer.Context,
    operation_id: UUID,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = False,
) -> None:
    client, machine, interval, timeout = _context(ctx)
    with client:
        result = client.request("GET", f"{API_PREFIX}/operations/{operation_id}")
        if wait:
            result = _poll(client, result, interval=interval, timeout=timeout, json_output=machine)
        _emit(
            result
            if machine
            else f"Operation {result['id']}: {result['type']} {result['status']}"
            + (f"\n{_failure(result)}" if result["status"] == "failed" else ""),
            json_output=machine,
        )
        if result["status"] == "failed":
            raise typer.Exit(EXIT_API)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
