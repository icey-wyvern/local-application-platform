"""Exercise the complete disposable CLI, build, rollback and runtime lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: S404 - fixed local CLI, kubectl and curl tools.
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UV = str(Path.home() / ".local/bin/uv")
KUBECTL = str(Path.home() / ".local/bin/kubectl")
KUBECONFIG = str(Path.home() / ".kube/lap-admin.conf")
CA = str(ROOT / ".generated/pki/lab-root-ca.crt")


class AcceptanceError(RuntimeError):
    pass


def run(label: str, *command: str, expected: int = 0) -> str:
    environment = dict(os.environ)
    environment["LAP_CREDENTIALS_FILE"] = str(
        Path.home() / ".local/share/local-application-platform/secrets/platform-api-token"
    )
    environment["LAP_CA_BUNDLE"] = CA
    environment["NO_PROXY"] = "api.lab.test"
    environment["no_proxy"] = "api.lab.test"
    result = subprocess.run(  # noqa: S603 - commands and fixture arguments are explicit.
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=1250,
    )
    if result.returncode != expected:
        raise AcceptanceError(f"{label}: exit {result.returncode}, expected {expected}")
    return result.stdout


def cli(label: str, *arguments: str, expected: int = 0) -> Any:
    output = run(
        label, UV, "run", "--package", "lap-cli", "lap", "--json", *arguments, expected=expected
    )
    return json.loads(output)


def assert_runtime(name: str, release: dict[str, Any]) -> None:
    resource = json.loads(
        run(
            "Application CR",
            KUBECTL,
            "--kubeconfig",
            KUBECONFIG,
            "get",
            "application",
            name,
            "-o",
            "json",
        )
    )
    status = resource["status"]
    if (
        resource["spec"]["releaseId"] != release["id"]
        or resource["spec"]["image"] != release["imageReference"]
        or status["activeReleaseId"] != release["id"]
        or status["readyReplicas"] != 2
        or status["phase"] != "Ready"
    ):
        raise AcceptanceError("controller did not reconcile the selected Release")
    route = json.loads(
        run(
            "HTTPRoute",
            KUBECTL,
            "--kubeconfig",
            KUBECONFIG,
            "-n",
            f"app-{name}",
            "get",
            "httproute",
            "application",
            "-o",
            "json",
        )
    )
    conditions = {
        condition["type"]: condition["status"]
        for parent in route["status"]["parents"]
        for condition in parent["conditions"]
    }
    if conditions.get("Accepted") != "True" or conditions.get("ResolvedRefs") != "True":
        raise AcceptanceError("HTTPRoute was not accepted")
    health = run(
        "trusted application HTTPS",
        "curl",
        "--noproxy",
        "*",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "15",
        "--cacert",
        CA,
        f"https://{name}.apps.lab.test{release['healthPath']}",
    )
    if health.strip() != "ok":
        raise AcceptanceError("trusted application HTTPS returned unexpected content")


def acceptance(repository: str, name: str) -> None:
    initial = cli("initial application list", "list")
    if any(item["name"] == name for item in initial):
        raise AcceptanceError("disposable application name is already in use")
    original_error: Exception | None = None
    try:
        first = cli("healthy deploy A", "deploy", repository, "--revision", "success")
        if first["application"]["name"] != name:
            raise AcceptanceError("fixture produced an unexpected Application name")
        release_a = first["release"]
        assert_runtime(name, release_a)
        second = cli("healthy deploy B", "deploy", repository, "--revision", "success-v2")
        release_b = second["release"]
        if (
            release_a["id"] == release_b["id"]
            or release_a["imageDigest"] == release_b["imageDigest"]
        ):
            raise AcceptanceError("second build did not create a distinct Release")
        assert_runtime(name, release_b)
        before = cli("history before historical activation", "history", name)
        activated = cli(
            "historical Release activation", "rollback", name, "--release", release_a["id"]
        )
        if activated["operation"]["status"] != "succeeded":
            raise AcceptanceError("historical Release activation did not succeed")
        after = cli("history after historical activation", "history", name)
        if len(after["builds"]) != len(before["builds"]) or len(after["releases"]) != len(
            before["releases"]
        ):
            raise AcceptanceError("historical activation created a Build or Release")
        manual = after["deployments"][0]
        if (
            manual["type"],
            manual["status"],
            manual["desiredReleaseId"],
            manual["previousReleaseId"],
        ) != ("manual_rollback", "healthy", release_a["id"], release_b["id"]):
            raise AcceptanceError("manual rollback history is inconsistent")
        assert_runtime(name, release_a)
        failed = cli(
            "unhealthy deploy and automatic rollback",
            "deploy",
            repository,
            "--revision",
            "unhealthy",
            expected=2,
        )
        operation = failed if "failureResult" in failed else failed.get("operation", {})
        if (
            operation.get("status") != "failed"
            or (operation.get("failureResult") or {}).get("rollbackStatus") != "succeeded"
            or (operation.get("failureResult") or {}).get("restoredReleaseId") != release_a["id"]
        ):
            raise AcceptanceError("automatic rollback did not restore the healthy Release")
        history = cli("history after automatic rollback", "history", name)
        if not any(
            item["type"] == "automatic_rollback"
            and item["status"] == "healthy"
            and item["desiredReleaseId"] == release_a["id"]
            for item in history["deployments"]
        ):
            raise AcceptanceError("automatic rollback Deployment history is missing")
        assert_runtime(name, release_a)
    except Exception as error:
        original_error = error
        raise
    finally:
        try:
            listed = cli("application list before cleanup", "list")
            if any(item["name"] == name for item in listed):
                deleted = cli("delete disposable application", "delete", name)
                if deleted["status"] != "succeeded":
                    raise AcceptanceError("disposable application deletion failed")
                remaining = cli("application list after cleanup", "list")
                if any(item["name"] == name for item in remaining):
                    raise AcceptanceError("disposable application remains after deletion")
        except Exception:
            if original_error is None:
                raise
            raise AcceptanceError(
                "acceptance failed and disposable cleanup also failed"
            ) from original_error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("application")
    arguments = parser.parse_args()
    acceptance(arguments.repository, arguments.application)
    print("PASS v1-end-to-end-acceptance")


if __name__ == "__main__":
    main()
