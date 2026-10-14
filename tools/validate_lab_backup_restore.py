"""Compact live backup/restore drill with a disposable public Git fixture."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # noqa: S404 - fixed repository CLI and curl commands only.
import tempfile
from pathlib import Path
from typing import Any

import lab_backup

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = Path.home() / ".local/share/local-application-platform/secrets/platform-api-token"
CA_FILE = ROOT / ".generated/pki/lab-root-ca.crt"


def command(*args: str) -> str:
    environment = dict(os.environ)
    environment["LAP_CREDENTIALS_FILE"] = str(TOKEN_FILE)
    environment["LAP_CA_BUNDLE"] = str(CA_FILE)
    result = subprocess.run(  # noqa: S603 - fixed tools and fixture arguments only.
        args, cwd=ROOT, env=environment, capture_output=True, check=False, text=True
    )
    if result.returncode:
        raise lab_backup.BackupError(f"validation command failed: {args[0]}")
    return result.stdout


def history(name: str) -> dict[str, Any]:
    value: object = json.loads(
        command("uv", "run", "--package", "lap-cli", "lap", "--json", "history", name)
    )
    if not isinstance(value, dict):
        raise lab_backup.BackupError("CLI history response is invalid")
    return value


def healthy(name: str) -> None:
    command(
        "curl",
        "--noproxy",
        "*",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "15",
        "--cacert",
        str(CA_FILE),
        f"https://{name}.apps.lab.test/health",
    )


def drill(repository: str, application: str, *, confirm_replace: bool) -> None:
    deployed = False
    try:
        command(
            "uv",
            "run",
            "--package",
            "lap-cli",
            "lap",
            "deploy",
            repository,
            "--revision",
            "success",
        )
        deployed = True
        before = history(application)
        releases = before.get("releases")
        deployments = before.get("deployments")
        if (
            not isinstance(releases, list)
            or not releases
            or not isinstance(deployments, list)
            or not deployments
            or deployments[-1].get("status") != "healthy"
        ):
            raise lab_backup.BackupError("disposable deployment has no healthy Release history")
        digest = releases[-1].get("imageDigest")
        release_id = releases[-1].get("id")
        if not isinstance(digest, str) or not isinstance(release_id, str):
            raise lab_backup.BackupError("Release digest or ID is missing")
        healthy(application)
        archive = lab_backup.backup()
        lab_backup.verify(archive)
        with tempfile.TemporaryDirectory(prefix=".tamper-", dir=lab_backup.BACKUPS) as raw:
            corrupted = Path(raw) / "corrupt.tar.zst"
            shutil.copyfile(archive, corrupted)
            with corrupted.open("r+b") as stream:
                stream.seek(corrupted.stat().st_size // 2)
                value = stream.read(1)
                stream.seek(corrupted.stat().st_size // 2)
                stream.write(bytes([value[0] ^ 1]))
            try:
                lab_backup.verify(corrupted)
            except (lab_backup.BackupError, OSError, ValueError):
                pass
            else:
                raise lab_backup.BackupError("tampered archive passed validation")
        lab_backup.acceptance(archive)
        if confirm_replace:
            lab_backup.restore(archive, confirm_replace=True)
            after = history(application)
            if after != before:
                raise lab_backup.BackupError("API/CLI history changed after restore")
            if after["releases"][-1]["imageDigest"] != digest:
                raise lab_backup.BackupError("restored image digest changed")
            if after["releases"][-1]["id"] != release_id:
                raise lab_backup.BackupError("restored Release ID changed")
            healthy(application)
    finally:
        if deployed:
            try:
                command("uv", "run", "--package", "lap-cli", "lap", "delete", application)
            except lab_backup.BackupError:
                # Keep the original failure; the operator can inspect the named fixture.
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("application")
    parser.add_argument("--confirm-replace", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        drill(args.repository, args.application, confirm_replace=args.confirm_replace)
    except (lab_backup.BackupError, OSError, ValueError) as error:
        parser.exit(1, f"FAIL lab-backup-restore-acceptance: {error}\n")
    print(
        "PASS lab-backup-restore-acceptance"
        if args.confirm_replace
        else "PASS lab-backup-isolated-acceptance"
    )


if __name__ == "__main__":
    main()
