"""Delete disposable build-agent fixture Applications through the platform CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404 - fixed repository CLI and kubectl tools only.
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UV = str(Path.home() / ".local/bin/uv")
KUBECTL = str(Path.home() / ".local/bin/kubectl")
KUBECONFIG = str(Path.home() / ".kube/lap-admin.conf")
NAME = re.compile(r"build-agent-success-[a-z0-9]+\Z")


def invoke(*args: str) -> str:
    environment = dict(os.environ)
    environment["LAP_CREDENTIALS_FILE"] = str(
        Path.home() / ".local/share/local-application-platform/secrets/platform-api-token"
    )
    environment["LAP_CA_BUNDLE"] = str(ROOT / ".generated/pki/lab-root-ca.crt")
    environment["NO_PROXY"] = "api.lab.test"
    environment["no_proxy"] = "api.lab.test"
    result = subprocess.run(  # noqa: S603 - arguments are fixed tools and validated fixture names.
        args,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=1500,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed with exit {result.returncode}")
    return result.stdout


def cli(*args: str) -> Any:
    return json.loads(invoke(UV, "run", "--package", "lap-cli", "lap", "--json", *args))


def delete(name: str) -> str:
    if NAME.fullmatch(name) is None:
        raise ValueError("refusing to delete a non-fixture Application")
    result = cli("delete", name)
    if result["status"] != "succeeded":
        raise RuntimeError(f"fixture deletion did not succeed: {name}")
    return name


def main() -> None:
    names = [item["name"] for item in cli("list") if NAME.fullmatch(item["name"])]
    with ThreadPoolExecutor(max_workers=2) as executor:
        completed = list(executor.map(delete, names))
    remaining = {item["name"] for item in cli("list")}
    if remaining.intersection(completed):
        raise RuntimeError("deleted fixture remains in the public Application list")
    resources = json.loads(
        invoke(
            KUBECTL,
            "--kubeconfig",
            KUBECONFIG,
            "get",
            "applications.platform.lab.test",
            "-o",
            "json",
        )
    )
    if any(NAME.fullmatch(item["metadata"]["name"]) for item in resources["items"]):
        raise RuntimeError("disposable Application CR remains")
    namespaces = json.loads(
        invoke(KUBECTL, "--kubeconfig", KUBECONFIG, "get", "namespaces", "-o", "json")
    )
    if any(
        item["metadata"]["name"].startswith("app-build-agent-success-")
        for item in namespaces["items"]
    ):
        raise RuntimeError("disposable Application namespace remains")
    print(f"PASS validation-application-cleanup {len(completed)}")


if __name__ == "__main__":
    main()
