#!/usr/bin/env python3
"""Check local validation evidence for the lab's actual credential bytes."""

from __future__ import annotations

import base64
import json
import re
import socket
import subprocess  # noqa: S404 - fixed local validation commands only.
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
KUBECTL = str(Path.home() / ".local/bin/kubectl")
KUBECONFIG = str(Path.home() / ".kube/lap-admin.conf")
SECRET_ROOT = Path.home() / ".local/share/local-application-platform/secrets"


def command(*arguments: str) -> bytes:
    result = subprocess.run(  # noqa: S603 - callers pass fixed repository-owned arguments.
        arguments, cwd=ROOT, capture_output=True, check=True
    )
    return result.stdout


def needles() -> list[bytes]:
    values: set[bytes] = set()
    for path in SECRET_ROOT.rglob("*"):
        if not path.is_file():
            continue
        value = path.read_bytes().strip()
        if len(value) >= 16:
            values.add(value)
            values.add(base64.b64encode(value))
    if not values:
        raise RuntimeError("no lab credentials were available for the leak scan")
    return sorted(values)


def scan(label: str, content: bytes, sensitive: list[bytes]) -> None:
    if any(value in content for value in sensitive):
        raise RuntimeError(f"credential bytes appeared in {label}")


def public_candidate_names() -> list[str]:
    listed = command("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [name.decode("utf-8", "surrogateescape") for name in listed.split(b"\0") if name]


def check_public_boundary(names: list[str], root: Path) -> None:
    """Reject private paths and recognizable credentials in publication candidates."""
    private_markers = tuple(
        b"-----BEGIN " + (kind + b" " if kind else b"") + b"PRIVATE KEY-----"
        for kind in (b"", b"RSA", b"EC", b"OPENSSH", b"ENCRYPTED")
    )
    token_patterns = (
        re.compile(rb"AKIA[0-9A-Z]{16}"),
        re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    )
    private_names = {"config/lab.yaml", "kubeconfig", "credentials", "credentials.json"}
    private_suffixes = {".key", ".pem", ".p12", ".pfx", ".tfstate", ".tfvars"}
    for name in names:
        normalized = name.replace("\\", "/")
        parts = normalized.split("/")
        if (
            normalized in private_names
            or any(part in {".generated", "backups", ".kube", ".terraform"} for part in parts)
            or any(part == ".env" or part.startswith(".env.") for part in parts)
            or Path(normalized).suffix.lower() in private_suffixes
            or normalized.endswith((".tfvars.json", ".tfstate.backup"))
        ):
            raise RuntimeError(f"private path is a public candidate: {name}")
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"public candidate is not a regular file: {name}")
        if path.stat().st_size >= 5_000_000:
            raise RuntimeError(f"oversized public candidate requires review: {name}")
        content = path.read_bytes()
        if any(marker in content for marker in private_markers) or any(
            pattern.search(content) for pattern in token_patterns
        ):
            raise RuntimeError(f"credential pattern in public candidate: {name}")


def port_forward(namespace: str, service: str, local_port: int) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(  # noqa: S603 - fixed kubectl path and service names only.
        [
            KUBECTL,
            "--kubeconfig",
            KUBECONFIG,
            "-n",
            namespace,
            "port-forward",
            f"service/{service}",
            f"{local_port}:{3100 if service == 'loki' else 3200 if service == 'tempo' else 8080}",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError(f"{service} port forward exited")
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=0.2):
                return process
        except OSError:
            time.sleep(0.1)
    process.terminate()
    raise RuntimeError(f"{service} port forward did not become ready")


def main() -> None:
    sensitive = needles()
    names = public_candidate_names()
    check_public_boundary(names, ROOT)
    for name in names:
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"public candidate is not a regular file: {name}")
        if path.stat().st_size >= 5_000_000:
            raise RuntimeError(f"oversized public candidate requires review: {name}")
        scan(f"public candidate {name}", path.read_bytes(), sensitive)
    for arguments in (("git", "status", "--short"), ("git", "diff"), ("git", "diff", "--cached")):
        scan("Git status or diff", command(*arguments), sensitive)
    for directory in Path(tempfile.gettempdir()).glob("lap-validation-*"):
        for path in directory.rglob("*"):
            if path.is_file() and path.stat().st_size < 10_000_000:
                scan("validation artifact", path.read_bytes(), sensitive)
    print(f"PASS {len(names)} public candidates, Git and validation artifact secret scan")

    for selector in ("platform-api", "platform-controller"):
        output = command(
            KUBECTL,
            "--kubeconfig",
            KUBECONFIG,
            "-n",
            "platform-system",
            "logs",
            "-l",
            f"app.kubernetes.io/name={selector}",
            "--all-containers=true",
            "--tail=2000",
        )
        scan(f"{selector} logs", output, sensitive)
    journal = command(
        "ssh",
        "-o",
        "BatchMode=yes",
        "-i",
        str(Path.home() / ".ssh/lap_lab_ed25519"),
        "ubuntu@10.20.0.51",
        "sudo",
        "-n",
        "journalctl",
        "-u",
        "lap-build-agent",
        "-u",
        "lap-registry",
        "--no-pager",
        "-o",
        "cat",
        "-n",
        "3000",
    )
    scan("build host journal", journal, sensitive)
    print("PASS platform and build host log secret scan")

    forward = port_forward("platform-system", "platform-api", 18085)
    try:
        with urlopen("http://127.0.0.1:18085/metrics", timeout=5) as response:
            scan("API metrics", response.read(), sensitive)
    finally:
        forward.terminate()
        forward.wait(timeout=5)
    metrics = command(
        "curl",
        "--noproxy",
        "*",
        "--fail",
        "--silent",
        "--show-error",
        "--cacert",
        str(ROOT / ".generated/pki/lab-root-ca.crt"),
        "--cert",
        str(ROOT / ".generated/pki/platform-api-build-agent-client.crt"),
        "--key",
        str(ROOT / ".generated/pki/platform-api-build-agent-client.key"),
        "https://build-agent.lab.test:8443/metrics",
    )
    scan("build-agent metrics", metrics, sensitive)
    print("PASS API and build-agent metrics secret scan")

    forward = port_forward("monitoring", "tempo", 18086)
    try:
        query = urlencode({"q": '{ resource.service.name = "platform-api" }', "limit": "100"})
        with urlopen(f"http://127.0.0.1:18086/api/search?{query}", timeout=10) as response:
            search = response.read()
        scan("Tempo search", search, sensitive)
        traces = json.loads(search).get("traces", [])
        if not traces:
            raise RuntimeError("Tempo returned no platform API traces to scan")
        for trace in traces:
            identifier = trace["traceID"]
            with urlopen(f"http://127.0.0.1:18086/api/traces/{identifier}", timeout=10) as response:
                scan("Tempo trace", response.read(), sensitive)
    finally:
        forward.terminate()
        forward.wait(timeout=5)
    print("PASS recent Tempo trace secret scan")


if __name__ == "__main__":
    if sys.argv[1:] == ["--public-only"]:
        names = public_candidate_names()
        check_public_boundary(names, ROOT)
        print(f"PASS {len(names)} public candidates passed boundary scan")
    elif len(sys.argv) == 1:
        main()
    else:
        raise SystemExit("usage: scan-secret-exposure.py [--public-only]")
