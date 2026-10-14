"""Create and validate portable lab state archives.

Run from the repository root through the Make targets. Credentials stay in the
existing lab secret stores; this archive deliberately contains no credentials.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess  # noqa: S404 - executes fixed local lab tools without a shell.
import tarfile
import tempfile
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import yaml

ROOT = Path(__file__).resolve().parent.parent
BACKUPS = ROOT / "backups"
KUBECTL = str(Path.home() / ".local/bin/kubectl")
KUBECONFIG = str(Path.home() / ".kube/lap-admin.conf")
SSH = ["ssh", "-o", "BatchMode=yes", "-i", str(Path.home() / ".ssh/lap_lab_ed25519")]
CONTENTS = (
    "postgresql.dump",
    "registry.tar",
    "config/lab.yaml",
    "config/versions.yaml",
    "generated/cluster/coredns.yaml",
    "generated/helm/cilium.values.yaml",
    "generated/helm/metallb.values.yaml",
    "generated/helm/envoy-gateway.values.yaml",
    "generated/platform/platform.env",
    "releases.json",
    "manifest.json",
)


class BackupError(RuntimeError):
    pass


def serialized[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        BACKUPS.mkdir(mode=0o700, exist_ok=True)
        with (BACKUPS / ".workflow.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BackupError("another lab backup or restore is in progress") from error
            try:
                return function(*args, **kwargs)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    return wrapped


def run(
    *args: str,
    input_file: BinaryIO | None = None,
    input_bytes: bytes | None = None,
    output_file: BinaryIO | None = None,
) -> str:
    result = subprocess.run(  # noqa: S603 - arguments are fixed lab tools and validated paths.
        args,
        stdin=input_file if input_bytes is None else None,
        input=input_bytes,
        stdout=output_file if output_file is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()[:400]
        raise BackupError(f"command failed: {args[0]} ({detail or 'no diagnostic'})")
    return result.stdout.decode().strip() if output_file is None else ""


def kubectl(*args: str) -> str:
    return run(KUBECTL, "--kubeconfig", KUBECONFIG, *args)


def remote(
    *args: str,
    output_file: BinaryIO | None = None,
    input_file: BinaryIO | None = None,
    input_bytes: bytes | None = None,
) -> str:
    return run(
        *SSH,
        "ubuntu@10.20.0.51",
        *args,
        output_file=output_file,
        input_file=input_file,
        input_bytes=input_bytes,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def config() -> dict[str, object]:
    value: object = yaml.safe_load((ROOT / "config/lab.yaml").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BackupError("lab configuration is invalid")
    return value


def registry_path() -> str:
    registry = config().get("registry")
    if not isinstance(registry, dict):
        raise BackupError("registry configuration is missing")
    path = registry.get("data_path")
    if not isinstance(path, str) or path != "/var/lib/lap-registry":
        raise BackupError("unsupported registry storage path")
    return path


def check_space(required: int, directory: Path) -> None:
    free = shutil.disk_usage(directory).free
    if free < required:
        raise BackupError("insufficient free space for backup staging and archive")


def restrict_host_backups() -> None:
    """Use NTFS ACLs because chmod alone is ineffective on this DrvFS mount."""
    if not str(BACKUPS).startswith("/mnt/"):
        return
    windows_path = run("wslpath", "-w", str(BACKUPS))
    script_path = run("wslpath", "-w", str(ROOT / "tools/restrict-backup-acl.ps1"))
    run(
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script_path,
        "-Path",
        windows_path,
    )


def database_counts() -> tuple[int, int, int]:
    query = (
        "SELECT (SELECT count(*) FROM operations WHERE status='running'),"
        "(SELECT count(*) FROM builds b JOIN operations o ON o.id=b.operation_id "
        "WHERE o.status='running' AND b.status IN "
        "('pending','cloning','validating','building','scanning','publishing')) ,"
        "(SELECT count(*) FROM deployments d JOIN operations o ON o.id=d.operation_id "
        "WHERE o.status='running' AND d.status IN "
        "('pending','applying','reconciling','verifying'))"
    )
    value = kubectl(
        "-n",
        "platform-database",
        "exec",
        "statefulset/postgresql",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "platform",
        "-At",
        "-c",
        query,
    )
    parts = [int(part) for part in value.split("|")]
    if len(parts) != 3:
        raise BackupError("unexpected database activity response")
    return parts[0], parts[1], parts[2]


def history_counts(database: str = "platform") -> list[int]:
    value = kubectl(
        "-n",
        "platform-database",
        "exec",
        "statefulset/postgresql",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        database,
        "-At",
        "-F",
        "|",
        "-c",
        "SELECT (SELECT count(*) FROM applications),"
        "(SELECT count(*) FROM builds),(SELECT count(*) FROM releases),"
        "(SELECT count(*) FROM deployments),(SELECT count(*) FROM operations),"
        "(SELECT count(*) FROM audit_events)",
    )
    result = [int(part) for part in value.split("|")]
    if len(result) != 6:
        raise BackupError("history count query returned an invalid row")
    return result


def scale_api(replicas: int) -> None:
    kubectl("-n", "platform-system", "scale", "deployment/platform-api", f"--replicas={replicas}")
    if replicas:
        kubectl(
            "-n", "platform-system", "rollout", "status", "deployment/platform-api", "--timeout=5m"
        )
    else:
        if kubectl(
            "-n",
            "platform-system",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=platform-api",
            "-o",
            "name",
        ):
            kubectl(
                "-n",
                "platform-system",
                "wait",
                "--for=delete",
                "pod",
                "-l",
                "app.kubernetes.io/name=platform-api",
                "--timeout=5m",
            )


@serialized
def backup() -> Path:
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    os.chmod(BACKUPS, 0o700)
    restrict_host_backups()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = BACKUPS / timestamp
    if destination.exists():
        raise BackupError("backup timestamp already exists")
    registry_path()
    registry_bytes = int(remote("sudo", "-n", "du", "-sb", "/var/lib/lap-registry").split()[0])
    database_bytes = int(
        kubectl(
            "-n",
            "platform-database",
            "exec",
            "statefulset/postgresql",
            "--",
            "psql",
            "-U",
            "postgres",
            "-d",
            "platform",
            "-At",
            "-c",
            "SELECT pg_database_size('platform')",
        )
    )
    check_space(3 * (registry_bytes + database_bytes) + 512 * 1024 * 1024, BACKUPS)
    old_replicas = int(
        kubectl(
            "-n",
            "platform-system",
            "get",
            "deployment/platform-api",
            "-o",
            "jsonpath={.spec.replicas}",
        )
    )
    if old_replicas < 1:
        raise BackupError("API is already stopped; maintenance ownership is unknown")
    if any(database_counts()):
        raise BackupError("active work exists; wait for completion before backup")
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=BACKUPS) as raw:
        stage = Path(raw)
        os.chmod(stage, 0o700)
        registry_stopped = False
        api_stopped = False
        try:
            # Stop new requests and allow scheduler shutdown to release leases.
            api_stopped = True
            scale_api(0)
            if any(database_counts()):
                raise BackupError("active operation/build/deployment remains after quiescing")
            remote("sudo", "-n", "systemctl", "stop", "lap-build-agent")
            registry_stopped = True
            remote("sudo", "-n", "systemctl", "stop", "lap-registry")
            with (stage / "postgresql.dump").open("wb") as stream:
                kubectl_args = (
                    KUBECTL,
                    "--kubeconfig",
                    KUBECONFIG,
                    "-n",
                    "platform-database",
                    "exec",
                    "statefulset/postgresql",
                    "--",
                    "pg_dump",
                    "-U",
                    "platform_api",
                    "-d",
                    "platform",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                )
                run(*kubectl_args, output_file=stream)
            with (stage / "registry.tar").open("wb") as stream:
                remote(
                    "sudo",
                    "-n",
                    "tar",
                    "-C",
                    "/var/lib/lap-registry",
                    "-cf",
                    "-",
                    ".",
                    output_file=stream,
                )
            snapshot_counts = history_counts()
            references = release_references("platform")
            (stage / "releases.json").write_text(
                json.dumps(references, sort_keys=True) + "\n", encoding="utf-8"
            )
            missing = missing_release_artifacts(stage / "registry.tar", "platform")
            if missing:
                raise BackupError(
                    f"{len(missing)} historical Release image references are absent from registry"
                )
        finally:
            if registry_stopped:
                remote("sudo", "-n", "systemctl", "start", "lap-registry")
            if api_stopped:
                scale_api(old_replicas)
            remote("sudo", "-n", "systemctl", "start", "lap-build-agent")
        for source, target in (
            ("config/lab.yaml", "config/lab.yaml"),
            ("config/versions.yaml", "config/versions.yaml"),
            (".generated/cluster/coredns.yaml", "generated/cluster/coredns.yaml"),
            (".generated/helm/cilium.values.yaml", "generated/helm/cilium.values.yaml"),
            (".generated/helm/metallb.values.yaml", "generated/helm/metallb.values.yaml"),
            (
                ".generated/helm/envoy-gateway.values.yaml",
                "generated/helm/envoy-gateway.values.yaml",
            ),
            (".generated/platform/platform.env", "generated/platform/platform.env"),
        ):
            destination_file = stage / target
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, destination_file)
        metadata = {
            "format": 1,
            "createdUtc": timestamp,
            "postgresql": yaml.safe_load((ROOT / "config/versions.yaml").read_text())["postgresql"],
            "registry": yaml.safe_load((ROOT / "config/versions.yaml").read_text())["registry"],
            "operationPolicy": "no running work; queued work resumes after restore",
            "historyCounts": snapshot_counts,
        }
        (stage / "manifest.json").write_text(
            json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8"
        )
        scan_known_secrets(stage)
        checksums = "".join(f"{sha256(stage / name)}  {name}\n" for name in CONTENTS)
        (stage / "SHA256SUMS").write_text(checksums, encoding="ascii")
        for path in stage.rglob("*"):
            if path.is_file():
                os.chmod(path, 0o600)
        archive = stage / "lab-backup.tar.zst"
        # Python 3.14's standard Zstandard codec keeps the format pinned to Python.
        zstd = importlib.import_module("compression.zstd")

        with archive.open("wb") as raw_archive, zstd.open(raw_archive, "wb") as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as tar:
                for name in (*CONTENTS, "SHA256SUMS"):
                    tar.add(stage / name, arcname=name, recursive=False)
        os.chmod(archive, 0o600)
        verify(archive)
        # Keep the indexed contents beside the portable compressed archive.
        os.rename(stage, destination)
    return destination / "lab-backup.tar.zst"


def verify(archive: Path, *, extract_to: Path | None = None) -> dict[str, object]:
    zstd = importlib.import_module("compression.zstd")

    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    restrict_host_backups()
    check_space(archive.stat().st_size * 3 + 512 * 1024 * 1024, BACKUPS)
    context = (
        tempfile.TemporaryDirectory(prefix=".verify-", dir=BACKUPS)
        if extract_to is None
        else nullcontext(str(extract_to))
    )
    with context as raw:
        stage = Path(raw)
        seen: set[str] = set()
        with archive.open("rb") as raw_archive, zstd.open(raw_archive, "rb") as compressed:
            with tarfile.open(fileobj=compressed, mode="r|") as tar:
                for member in tar:
                    name = member.name
                    if (
                        name in seen
                        or name not in (*CONTENTS, "SHA256SUMS")
                        or not member.isfile()
                        or member.size > 50 * 1024**3
                    ):
                        raise BackupError("unsafe or unexpected archive member")
                    if member.size + 128 * 1024 * 1024 > shutil.disk_usage(stage).free:
                        raise BackupError("insufficient free space to validate archive member")
                    seen.add(name)
                    target = stage / PurePosixPath(name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is None:
                        raise BackupError("archive member could not be read")
                    with target.open("xb") as output:
                        shutil.copyfileobj(source, output)
        if seen != set((*CONTENTS, "SHA256SUMS")):
            raise BackupError("backup archive is incomplete")
        expected = "".join(f"{sha256(stage / name)}  {name}\n" for name in CONTENTS)
        if (stage / "SHA256SUMS").read_text(encoding="ascii") != expected:
            raise BackupError("backup checksum mismatch")
        metadata: dict[str, object] = json.loads((stage / "manifest.json").read_text())
        if metadata.get("format") != 1:
            raise BackupError("unsupported backup format")
        counts = metadata.get("historyCounts")
        if (
            not isinstance(counts, list)
            or len(counts) != 6
            or not all(isinstance(count, int) and count >= 0 for count in counts)
        ):
            raise BackupError("backup history counts are invalid")
        if (stage / "config/versions.yaml").read_bytes() != (
            ROOT / "config/versions.yaml"
        ).read_bytes():
            raise BackupError("project component versions do not match")
        if (stage / "config/lab.yaml").read_bytes() != (ROOT / "config/lab.yaml").read_bytes():
            raise BackupError("authoritative lab configuration does not match")
        validate_registry_tar(stage / "registry.tar")
        validate_release_inventory(stage)
        return metadata


def scan_known_secrets(stage: Path) -> None:
    secret_root = Path.home() / ".local/share/local-application-platform/secrets"
    needles: set[bytes] = set()
    for secret in secret_root.rglob("*"):
        if secret.is_file():
            value = secret.read_bytes().strip()
            if len(value) >= 16:
                needles.add(value)
                needles.add(base64.b64encode(value))
    if not needles:
        raise BackupError("known-secret scan has no credential inputs")
    overlap = max(map(len, needles)) - 1
    for name in CONTENTS:
        path = stage / name
        if not path.is_file():
            raise BackupError("backup staging file is missing")
        with path.open("rb") as stream:
            previous = b""
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                data = previous + block
                if any(needle in data for needle in needles):
                    raise BackupError("known credential bytes appeared in backup contents")
                previous = data[-overlap:]


def validate_release_inventory(stage: Path) -> None:
    inventory = json.loads((stage / "releases.json").read_text(encoding="utf-8"))
    if not isinstance(inventory, list):
        raise BackupError("Release inventory is invalid")
    with tarfile.open(stage / "registry.tar", "r:") as registry:
        members = {member.name: member for member in registry}
        for row in inventory:
            if not isinstance(row, list) or len(row) != 2:
                raise BackupError("Release inventory row is invalid")
            reference, digest = row
            if not isinstance(reference, str) or not isinstance(digest, str):
                raise BackupError("Release inventory reference is invalid")
            match = re.fullmatch(
                r"registry\.lab\.test:5000/([a-z0-9/-]+)@sha256:([0-9a-f]{64})",
                reference,
            )
            if match is None or digest != f"sha256:{match.group(2)}":
                raise BackupError("Release inventory digest is invalid")
            link = (
                f"./docker/registry/v2/repositories/{match.group(1)}"
                f"/_manifests/revisions/sha256/{match.group(2)}/link"
            )
            member = members.get(link)
            if member is None or not member.isfile():
                raise BackupError("historical Release image is absent from registry archive")
            source = registry.extractfile(member)
            if source is None or source.read() != digest.encode():
                raise BackupError("historical Release registry link is corrupt")
            blob = f"./docker/registry/v2/blobs/sha256/{match.group(2)[:2]}/{match.group(2)}/data"
            blob_member = members.get(blob)
            if blob_member is None or not blob_member.isfile():
                raise BackupError("historical manifest blob is absent")
            blob_source = registry.extractfile(blob_member)
            if blob_source is None or hashlib.sha256(blob_source.read()).hexdigest() != match.group(
                2
            ):
                raise BackupError("historical manifest blob checksum mismatch")


def validate_registry_tar(path: Path) -> None:
    with tarfile.open(path, "r:") as tar:
        for member in tar:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise BackupError("registry archive contains an unsafe entry")


def release_references(database: str = "platform") -> list[tuple[str, str]]:
    value = kubectl(
        "-n",
        "platform-database",
        "exec",
        "statefulset/postgresql",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        database,
        "-At",
        "-F",
        "|",
        "-c",
        "SELECT image_reference, image_digest FROM releases ORDER BY created_at",
    )
    if not value:
        return []
    rows: list[tuple[str, str]] = []
    for row in value.splitlines():
        parts = row.split("|", 1)
        if len(parts) != 2:
            raise BackupError("Release inventory query returned an invalid row")
        rows.append((parts[0], parts[1]))
    return rows


def missing_release_artifacts(registry_archive: Path, database: str) -> list[str]:
    with tarfile.open(registry_archive, "r:") as registry:
        members = set(registry.getnames())
    missing: list[str] = []
    for reference, digest in release_references(database):
        match = re.fullmatch(
            r"registry\.lab\.test:5000/([a-z0-9/-]+)@sha256:([0-9a-f]{64})",
            reference,
        )
        if match is None or digest != f"sha256:{match.group(2)}":
            missing.append(reference)
            continue
        link = (
            f"./docker/registry/v2/repositories/{match.group(1)}"
            f"/_manifests/revisions/sha256/{match.group(2)}/link"
        )
        if link not in members:
            missing.append(reference)
    return missing


def repair_historical_release_links(registry_archive: Path) -> int:
    """Recover missing Distribution links only when the exact blob still exists."""
    missing = missing_release_artifacts(registry_archive, "platform")
    if any(database_counts()):
        raise BackupError("active work exists; historical registry repair requires quiescence")
    entries: list[tuple[str, str]] = []
    for reference in missing:
        match = re.fullmatch(
            r"registry\.lab\.test:5000/([a-z0-9/-]+)@sha256:([0-9a-f]{64})",
            reference,
        )
        if match is None:
            raise BackupError("a missing Release reference is malformed")
        digest = match.group(2)
        blob = f"/var/lib/lap-registry/docker/registry/v2/blobs/sha256/{digest[:2]}/{digest}/data"
        if remote("sudo", "-n", "sha256sum", blob).split()[0] != digest:
            raise BackupError("historical manifest blob is absent or corrupt")
        link = (
            f"/var/lib/lap-registry/docker/registry/v2/repositories/{match.group(1)}"
            f"/_manifests/revisions/sha256/{digest}/link"
        )
        entries.append((link, digest))
    if not entries:
        return 0
    remote("sudo", "-n", "systemctl", "stop", "lap-build-agent")
    try:
        remote("sudo", "-n", "systemctl", "stop", "lap-registry")
        try:
            for link, digest in entries:
                parent = str(Path(link).parent)
                remote(
                    "sudo",
                    "-n",
                    "install",
                    "-d",
                    "-m",
                    "0755",
                    "-o",
                    "lap-registry",
                    "-g",
                    "lap-registry",
                    parent,
                )
                remote("sudo", "-n", "tee", link, input_bytes=f"sha256:{digest}".encode())
                remote("sudo", "-n", "chown", "lap-registry:lap-registry", link)
        finally:
            remote("sudo", "-n", "systemctl", "start", "lap-registry")
    finally:
        remote("sudo", "-n", "systemctl", "start", "lap-build-agent")
    return len(missing)


def database_record_count() -> int:
    value = kubectl(
        "-n",
        "platform-database",
        "exec",
        "statefulset/postgresql",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "platform",
        "-At",
        "-c",
        "SELECT (SELECT count(*) FROM applications) + "
        "(SELECT count(*) FROM operations) + (SELECT count(*) FROM releases)",
    )
    return int(value)


def restored_applications() -> list[dict[str, object]]:
    query = (
        "SELECT coalesce(json_agg(json_build_object('name', a.name, "
        "'releaseId', r.id, 'image', r.image_reference, 'port', r.port, "
        "'healthPath', r.health_path))::text, '[]') "
        "FROM applications a JOIN releases r ON r.id=a.current_release_id "
        "WHERE a.deleted_at IS NULL"
    )
    value = kubectl(
        "-n",
        "platform-database",
        "exec",
        "statefulset/postgresql",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "platform",
        "-At",
        "-c",
        query,
    )
    result = json.loads(value)
    if not isinstance(result, list):
        raise BackupError("restored application list is invalid")
    return result


def acceptance(archive: Path) -> None:
    """Exercise dump and registry extraction in disposable isolated targets."""
    token = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    database = f"lap_restore_validation_{token}"
    directory = f"/var/lib/lap-registry-validation-{token}"
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".acceptance-", dir=BACKUPS) as raw:
        stage = Path(raw)
        verify(archive, extract_to=stage)
        created_database = False
        created_directory = False
        try:
            kubectl(
                "-n",
                "platform-database",
                "exec",
                "statefulset/postgresql",
                "--",
                "createdb",
                "-U",
                "postgres",
                "-O",
                "platform_api",
                "-T",
                "template0",
                database,
            )
            created_database = True
            with (stage / "postgresql.dump").open("rb") as source:
                run(
                    KUBECTL,
                    "--kubeconfig",
                    KUBECONFIG,
                    "-n",
                    "platform-database",
                    "exec",
                    "-i",
                    "statefulset/postgresql",
                    "--",
                    "pg_restore",
                    "-U",
                    "platform_api",
                    "-d",
                    database,
                    "--no-owner",
                    "--no-acl",
                    "--single-transaction",
                    "--exit-on-error",
                    input_file=source,
                )
            remote("sudo", "-n", "install", "-d", "-m", "0750", directory)
            created_directory = True
            with (stage / "registry.tar").open("rb") as source:
                remote("sudo", "-n", "tar", "-C", directory, "-xf", "-", input_file=source)
            rows = release_references(database)
            if not rows:
                raise BackupError("restored database has no historical releases")
            metadata = json.loads((stage / "manifest.json").read_text())
            if history_counts(database) != metadata["historyCounts"]:
                raise BackupError("isolated restore history counts do not match backup")
            for reference, digest in rows:
                match = re.fullmatch(
                    r"registry\.lab\.test:5000/([a-z0-9/-]+)@sha256:([0-9a-f]{64})",
                    reference,
                )
                if match is None or digest != f"sha256:{match.group(2)}":
                    raise BackupError("restored release has an invalid image reference")
                link = (
                    f"{directory}/docker/registry/v2/repositories/{match.group(1)}"
                    f"/_manifests/revisions/sha256/{match.group(2)}/link"
                )
                if remote("sudo", "-n", "cat", link) != digest:
                    raise BackupError("restored registry manifest digest does not match")
        finally:
            if created_database:
                kubectl(
                    "-n",
                    "platform-database",
                    "exec",
                    "statefulset/postgresql",
                    "--",
                    "dropdb",
                    "-U",
                    "postgres",
                    "--if-exists",
                    database,
                )
            if created_directory:
                remote("sudo", "-n", "rm", "-rf", "--", directory)


def reconcile_applications(stage: Path) -> set[str]:
    names: set[str] = set()
    for application in restored_applications():
        if not isinstance(application, dict):
            raise BackupError("restored application data is invalid")
        name = application.get("name")
        if not isinstance(name, str):
            raise BackupError("restored application name is invalid")
        names.add(name)
        document = {
            "apiVersion": "platform.lab.test/v1alpha1",
            "kind": "Application",
            "metadata": {"name": name},
            "spec": {
                "releaseId": application["releaseId"],
                "image": application["image"],
                "port": application["port"],
                "healthPath": application["healthPath"],
                "replicas": 2,
            },
        }
        manifest = stage / "restore-application.json"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        kubectl(
            "apply",
            "--server-side",
            "--field-manager=lap-restore",
            "--force-conflicts",
            "-f",
            str(manifest),
        )
        manifest.unlink()
    return names


def verify_live_release_links() -> None:
    for reference, digest in release_references():
        match = re.fullmatch(
            r"registry\.lab\.test:5000/([a-z0-9/-]+)@sha256:([0-9a-f]{64})",
            reference,
        )
        if match is None or digest != f"sha256:{match.group(2)}":
            raise BackupError("restored Release image reference is invalid")
        link = (
            f"/var/lib/lap-registry/docker/registry/v2/repositories/{match.group(1)}"
            f"/_manifests/revisions/sha256/{match.group(2)}/link"
        )
        if remote("sudo", "-n", "cat", link) != digest:
            raise BackupError("restored Release manifest digest is unavailable")


def preflight(archive: Path) -> Path:
    """Record read-only production facts before an approved replacement."""
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    restrict_host_backups()
    with tempfile.TemporaryDirectory(prefix=".preflight-", dir=BACKUPS) as raw:
        stage = Path(raw)
        metadata = verify(archive, extract_to=stage)
        scan_known_secrets(stage)
        with (stage / "postgresql.dump").open("rb") as source:
            listing = run(
                KUBECTL,
                "--kubeconfig",
                KUBECONFIG,
                "-n",
                "platform-database",
                "exec",
                "-i",
                "statefulset/postgresql",
                "--",
                "pg_restore",
                "--list",
                input_file=source,
            )
        if "TABLE DATA" not in listing:
            raise BackupError("PostgreSQL custom dump has no table data")
        references = json.loads((stage / "releases.json").read_text(encoding="utf-8"))
        if not isinstance(references, list) or not references:
            raise BackupError("backup has no historical Release digest to verify")
        selected = [references[0], *references[-2:]]
        samples: list[dict[str, str]] = []
        for row in selected:
            reference, digest = row
            match = re.fullmatch(
                r"registry\.lab\.test:5000/([a-z0-9/-]+)@sha256:([0-9a-f]{64})",
                reference,
            )
            if match is None or digest != f"sha256:{match.group(2)}":
                raise BackupError("representative Release reference is invalid")
            link = (
                f"/var/lib/lap-registry/docker/registry/v2/repositories/{match.group(1)}"
                f"/_manifests/revisions/sha256/{match.group(2)}/link"
            )
            if remote("sudo", "-n", "cat", link) != digest:
                raise BackupError("representative live registry digest is unavailable")
            samples.append({"reference": reference, "digest": digest})
        record = {
            "checkedUtc": datetime.now(UTC).isoformat(),
            "archiveSha256": sha256(archive),
            "archiveBytes": archive.stat().st_size,
            "backupHistoryCounts": metadata["historyCounts"],
            "currentHistoryCounts": history_counts(),
            "representativeDigests": samples,
            "checks": [
                "archive_structure",
                "checksums",
                "versions",
                "pg_restore_list",
                "registry_tar",
                "release_blobs",
                "known_secret_scan",
                "live_registry_samples",
            ],
        }
        output = archive.parent / "preflight.json"
        output.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(output, 0o600)
        restrict_host_backups()
        return output


@serialized
def restore(archive: Path, *, confirm_replace: bool, resume: bool = False) -> None:
    registry_path()
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=BACKUPS) as raw:
        stage = Path(raw)
        metadata = verify(archive, extract_to=stage)
        # pg_restore --list parses the custom dump without touching the database.
        with (stage / "postgresql.dump").open("rb") as source:
            run(
                KUBECTL,
                "--kubeconfig",
                KUBECONFIG,
                "-n",
                "platform-database",
                "exec",
                "-i",
                "statefulset/postgresql",
                "--",
                "pg_restore",
                "--list",
                input_file=source,
            )
        existing = database_record_count()
        existing_crs = kubectl("get", "applications.platform.lab.test", "-o", "name")
        if (existing or existing_crs) and not confirm_replace:
            raise BackupError(
                "target contains durable state; pass --confirm-replace after reviewing replacement"
            )
        if any(database_counts()):
            raise BackupError("target has active work")
        old_replicas = int(
            kubectl(
                "-n",
                "platform-system",
                "get",
                "deployment/platform-api",
                "-o",
                "jsonpath={.spec.replicas}",
            )
        )
        if old_replicas < 1 and not resume:
            raise BackupError("API is already stopped; maintenance ownership is unknown")
        target_replicas = old_replicas if old_replicas > 0 else 2
        token = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        staging = f"/var/lib/lap-registry.restore-{token}"
        previous = f"/var/lib/lap-registry.previous-{token}"
        api_stopped = old_replicas == 0
        registry_stopped = False
        moved_old = False
        swapped = False
        database_restored = False
        phase = "maintenance"
        try:
            if old_replicas > 0:
                api_stopped = True
                scale_api(0)
            if any(database_counts()):
                raise BackupError("active work remains after quiescing")
            phase = "registry staging"
            remote("sudo", "-n", "systemctl", "stop", "lap-build-agent")
            registry_stopped = True
            remote("sudo", "-n", "systemctl", "stop", "lap-registry")
            remote(
                "sudo",
                "-n",
                "install",
                "-d",
                "-m",
                "0750",
                "-o",
                "lap-registry",
                "-g",
                "lap-registry",
                staging,
            )
            with (stage / "registry.tar").open("rb") as source:
                remote("sudo", "-n", "tar", "-C", staging, "-xf", "-", input_file=source)
            remote("sudo", "-n", "mv", "/var/lib/lap-registry", previous)
            moved_old = True
            remote("sudo", "-n", "mv", staging, "/var/lib/lap-registry")
            swapped = True
            phase = "PostgreSQL restore"
            with (stage / "postgresql.dump").open("rb") as source:
                run(
                    KUBECTL,
                    "--kubeconfig",
                    KUBECONFIG,
                    "-n",
                    "platform-database",
                    "exec",
                    "-i",
                    "statefulset/postgresql",
                    "--",
                    "pg_restore",
                    "-U",
                    "platform_api",
                    "-d",
                    "platform",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-acl",
                    "--single-transaction",
                    "--exit-on-error",
                    input_file=source,
                )
            database_restored = True
            if history_counts() != metadata["historyCounts"]:
                raise BackupError("restored database history counts do not match backup")
            if [list(row) for row in release_references()] != json.loads(
                (stage / "releases.json").read_text(encoding="utf-8")
            ):
                raise BackupError("restored Release inventory does not match backup")
            phase = "Alembic validation"
            # The schema check is read-only after pg_restore commits.
            run(
                "bash",
                "tools/migrate-platform-api.sh",
                KUBECTL,
                KUBECONFIG,
                str(Path.home() / ".local/share/local-application-platform/secrets"),
                ".generated/pki/lab-root-ca.crt",
            )
            phase = "controller reconciliation"
            kubectl(
                "apply",
                "--server-side",
                "--field-manager=lap-kustomize",
                "--kustomize",
                "cluster/platform-controller",
            )
            phase = "Application reconciliation"
            desired_names = reconcile_applications(stage)
            if confirm_replace:
                for resource in existing_crs.splitlines():
                    name = resource.removeprefix("application.platform.lab.test/")
                    if name not in desired_names:
                        kubectl("delete", resource, "--wait=true")
            phase = "registry digest verification"
            remote("sudo", "-n", "systemctl", "start", "lap-registry")
            registry_stopped = False
            verify_live_release_links()
            phase = "API reconciliation"
            kubectl(
                "apply",
                "--server-side",
                "--field-manager=lap-kustomize",
                "--kustomize",
                "cluster/platform-api",
            )
            scale_api(target_replicas)
            api_stopped = False
            phase = "API readiness"
            status = run(
                "curl",
                "--silent",
                "--show-error",
                "--noproxy",
                "*",
                "--max-time",
                "15",
                "--cacert",
                ".generated/pki/lab-root-ca.crt",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                "https://api.lab.test/api/v1/info",
            )
            if status != "401":
                raise BackupError(f"public API returned unexpected HTTP status {status}")
            remote("sudo", "-n", "systemctl", "start", "lap-build-agent")
            try:
                remote("sudo", "-n", "rm", "-rf", "--", previous)
            except BackupError:
                pass  # The old registry directory can be removed after recovery.
        except Exception as error:
            if swapped and not database_restored:
                remote("sudo", "-n", "mv", "/var/lib/lap-registry", staging)
                remote("sudo", "-n", "mv", previous, "/var/lib/lap-registry")
            elif moved_old and not swapped:
                remote("sudo", "-n", "mv", previous, "/var/lib/lap-registry")
            if database_restored:
                scale_api(0)
                raise BackupError(
                    f"restore committed but {phase} failed: {error}; API remains stopped"
                ) from error
            raise
        finally:
            if registry_stopped:
                remote("sudo", "-n", "systemctl", "start", "lap-registry")
            if api_stopped and not database_restored and old_replicas > 0:
                scale_api(old_replicas)
            remote("sudo", "-n", "systemctl", "start", "lap-build-agent")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("backup")
    verification = command.add_parser("verify")
    verification.add_argument("archive", type=Path)
    restoration = command.add_parser("restore")
    restoration.add_argument("archive", type=Path)
    restoration.add_argument("--confirm-replace", action="store_true")
    restoration.add_argument("--resume", action="store_true")
    acceptance_command = command.add_parser("acceptance")
    acceptance_command.add_argument("archive", type=Path)
    audit_command = command.add_parser("audit")
    audit_command.add_argument("registry_archive", type=Path)
    audit_command.add_argument("--list-missing", action="store_true")
    repair_command = command.add_parser("repair-links")
    repair_command.add_argument("registry_archive", type=Path)
    preflight_command = command.add_parser("preflight")
    preflight_command.add_argument("archive", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "backup":
            result = backup()
            print(f"PASS lab-backup {result}")
        elif args.command == "verify":
            verify(args.archive)
            print("PASS lab-backup-verify")
        elif args.command == "acceptance":
            acceptance(args.archive)
            print("PASS lab-backup-restore-isolated")
        elif args.command == "audit":
            missing = missing_release_artifacts(args.registry_archive, "platform")
            if missing:
                if args.list_missing:
                    for reference in missing:
                        print(reference)
                raise BackupError(f"{len(missing)} historical Release artifacts are missing")
            print("PASS lab-registry-release-audit")
        elif args.command == "repair-links":
            repaired = repair_historical_release_links(args.registry_archive)
            print(f"PASS lab-registry-links-repaired {repaired}")
        elif args.command == "preflight":
            record = preflight(args.archive)
            print(f"PASS lab-restore-preflight {record}")
        else:
            restore(args.archive, confirm_replace=args.confirm_replace, resume=args.resume)
            print("PASS lab-restore")
    except (BackupError, OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"FAIL lab-{args.command}: {error}\n")


if __name__ == "__main__":
    main()
