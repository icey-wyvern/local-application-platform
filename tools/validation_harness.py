#!/usr/bin/env python3
"""Run one live validation quietly and retain scoped diagnostics on failure."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess  # noqa: S404 - subprocess runs the explicitly supplied validation command.
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

SENSITIVE_ENV = re.compile(r"TOKEN|PASSWORD|SECRET|CREDENTIAL|PRIVATE|AUTH", re.IGNORECASE)
BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*")
KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
SENSITIVE_FIELD = re.compile(
    r'(?i)(["\']?(?:password|token|secret|authorization|private[_ -]?key|client[_ -]?key)'
    r'["\']?\s*[:=]\s*["\']?)([^\s,"\'}]+)'
)


def redact(value: str, secrets: Sequence[str]) -> str:
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        value = value.replace(secret, "<redacted>")
    value = KEY_BLOCK.sub("<redacted private key>", value)
    value = BEARER.sub(r"\1<redacted>", value)
    return SENSITIVE_FIELD.sub(r"\1<redacted>", value)


def secret_values(environment: dict[str, str]) -> list[str]:
    return [value for key, value in environment.items() if SENSITIVE_ENV.search(key) and value]


def failure_kind(*, actual: str, timed_out: bool, stdout: str, stderr: str) -> str | None:
    if actual == "exit 0":
        return None
    if timed_out:
        return "timeout"
    output = f"{stdout}\n{stderr}".lower()
    if any(
        marker in output
        for marker in (
            "connection refused",
            "connection reset",
            "no route to host",
            "unable to connect to the server",
            "context deadline exceeded",
            "could not resolve",
            "temporary failure in name resolution",
            "tls handshake timeout",
            "server is currently unable to handle the request",
        )
    ):
        return "infrastructure"
    if any(
        marker in output
        for marker in ("assertionerror", "assertion failed", "assertion:", "expected:")
    ):
        return "assertion"
    return "command_failure"


def kubectl_snapshot(
    *,
    artifact: Path,
    kubectl: str,
    kubeconfig: str,
    namespace: str | None,
    deployment: str | None,
    selector: str | None,
    application: str | None,
    logs_selector: str | None,
    secrets: Sequence[str],
) -> None:
    """Save only named resources and namespace-scoped warning events; never read Secrets."""

    commands: list[tuple[str, list[str]]] = []
    base = [kubectl, "--kubeconfig", kubeconfig]
    if application:
        commands.append((
            "application.yaml",
            [*base, "get", "application", application, "-o", "yaml"],
        ))
        namespace = f"app-{application}"
        selector = f"platform.lab.test/application={application}"
    if namespace:
        scoped = [*base, "--namespace", namespace]
        if application:
            for resource in (
                "deployment/application",
                "service/application",
                "httproute/application",
            ):
                commands.append((
                    f"{resource.split('/')[0]}.yaml",
                    [*scoped, "get", resource, "-o", "yaml"],
                ))
        if deployment:
            commands.append((
                "deployment.yaml",
                [*scoped, "get", "deployment", deployment, "-o", "yaml"],
            ))
        if selector:
            commands.append((
                "pods.yaml",
                [*scoped, "get", "pods", "--selector", selector, "-o", "yaml"],
            ))
        commands.append((
            "warning-events.txt",
            [
                *scoped,
                "get",
                "events",
                "--field-selector=type=Warning",
                "--sort-by=.lastTimestamp",
                "-o",
                "wide",
            ],
        ))
        if logs_selector:
            try:
                pod_result = subprocess.run(  # noqa: S603 - configured kubectl executable only.
                    [*scoped, "get", "pods", "--selector", logs_selector, "-o", "json"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                pod_document = json.loads(pod_result.stdout or "{}")
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
                pod_document = {}
            for pod in pod_document.get("items", [])[:4]:
                pod_name = pod.get("metadata", {}).get("name", "")
                if re.fullmatch(r"[a-z0-9.-]{1,253}", pod_name):
                    commands.append((
                        f"logs-{pod_name}.txt",
                        [
                            *scoped,
                            "logs",
                            pod_name,
                            "--all-containers=true",
                            "--since=10m",
                            "--tail=200",
                        ],
                    ))

    output_dir = artifact / "kubernetes"
    output_dir.mkdir(mode=0o700, exist_ok=True)
    for filename, command in commands:
        try:
            result = subprocess.run(  # noqa: S603 - configured kubectl executable only.
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            content = result.stdout + ("\n" + result.stderr if result.stderr else "")
            (output_dir / filename).write_text(redact(content, secrets), encoding="utf-8")
        except (OSError, subprocess.TimeoutExpired) as error:
            (output_dir / f"{filename}.error.txt").write_text(
                redact(f"diagnostic collection failed: {error}\n", secrets), encoding="utf-8"
            )


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--kubectl")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--namespace")
    parser.add_argument("--deployment")
    parser.add_argument("--selector")
    parser.add_argument("--application")
    parser.add_argument("--logs-selector")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    if parsed.timeout <= 0 or not parsed.command:
        parser.error("--timeout must be positive and a command is required after --")
    if parsed.kubectl and not parsed.kubeconfig:
        parser.error("--kubeconfig is required when --kubectl is set")
    return parsed


def main(arguments: Sequence[str] | None = None) -> int:
    options = parse_arguments(arguments)
    command = options.command[1:] if options.command[0] == "--" else options.command
    if not command:
        print("FAIL validation command is empty", file=sys.stderr)
        return 2

    artifact = Path(tempfile.mkdtemp(prefix="lap-validation-"))
    artifact.chmod(0o700)
    stdout_path = artifact / "stdout.log"
    stderr_path = artifact / "stderr.log"
    environment = os.environ.copy()
    secrets = secret_values(environment)
    started_at = datetime.now(UTC)
    started = time.monotonic()
    outcome = "failed"
    actual = "not started"
    return_code = 1
    timed_out = False
    process: subprocess.Popen[bytes] | None = None
    try:
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with (
            os.fdopen(stdout_fd, "w", encoding="utf-8") as stdout,
            os.fdopen(stderr_fd, "w", encoding="utf-8") as stderr,
        ):
            process = subprocess.Popen(  # noqa: S603 - explicit validator argv is the contract.
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                start_new_session=(os.name != "nt"),
            )
            try:
                return_code = process.wait(timeout=options.timeout)
                actual = f"exit {return_code}"
            except subprocess.TimeoutExpired:
                timed_out = True
                actual = f"timeout after {options.timeout:g}s"
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait()
                return_code = 1
        for path in (stdout_path, stderr_path):
            path.write_text(redact(path.read_text(encoding="utf-8"), secrets), encoding="utf-8")
        if return_code == 0:
            outcome = "passed"
            actual = "exit 0"
        if outcome != "passed" and options.kubectl:
            kubectl_snapshot(
                artifact=artifact,
                kubectl=options.kubectl,
                kubeconfig=options.kubeconfig,
                namespace=options.namespace,
                deployment=options.deployment,
                selector=options.selector,
                application=options.application,
                logs_selector=options.logs_selector,
                secrets=secrets,
            )
    except KeyboardInterrupt:
        outcome = "interrupted"
        actual = "interrupted by user"
        return_code = 1
        if process is not None and process.poll() is None:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGINT)
            else:
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
    except OSError as error:
        actual = f"could not start validator: {type(error).__name__}"
        return_code = 1
    finally:
        captured_stdout = redact(
            stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "", secrets
        )
        captured_stderr = redact(
            stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "", secrets
        )
        for path in (stdout_path, stderr_path):
            if path.exists():
                path.write_text(
                    captured_stdout if path == stdout_path else captured_stderr, encoding="utf-8"
                )
        result = {
            "stage": options.stage,
            "expected": "exit 0",
            "actual": actual,
            "outcome": outcome,
            "timed_out": timed_out,
            "failure_kind": failure_kind(
                actual=actual,
                timed_out=timed_out,
                stdout=captured_stdout,
                stderr=captured_stderr,
            ),
            "started_at": started_at.isoformat(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "resource_context": {
                key: value
                for key, value in {
                    "namespace": options.namespace
                    or (f"app-{options.application}" if options.application else None),
                    "deployment": options.deployment,
                    "selector": options.selector,
                    "application": options.application,
                }.items()
                if value
            },
        }
        (artifact / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        for path in artifact.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
            elif path.is_dir():
                path.chmod(0o700)

    if outcome == "passed":
        temporary_root = Path(tempfile.gettempdir()).resolve()
        resolved_artifact = artifact.resolve()
        if resolved_artifact.parent != temporary_root or not resolved_artifact.name.startswith(
            "lap-validation-"
        ):
            raise RuntimeError("refusing to remove an unexpected validation artifact path")
        shutil.rmtree(resolved_artifact)
        print(f"PASS {options.stage}")
        return 0
    print(f"FAIL {options.stage}: expected exit 0, actual {actual}", file=sys.stderr)
    print(f"Diagnostics: {artifact}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
