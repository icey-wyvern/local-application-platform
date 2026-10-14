import base64
import fcntl
import importlib
import io
import json
import os
import subprocess  # noqa: S404 - runs the fixed repository script with test-only tools.
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import lab_backup  # noqa: E402 - tests import this repository script from its tools directory.


def archive_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(lab_backup, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(lab_backup, "ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/versions.yaml").write_text("postgresql: 18.6\nregistry: 3.0.0\n")
    (tmp_path / "config/lab.yaml").write_text("registry: {data_path: /var/lib/lap-registry}\n")
    content = tmp_path / "content"
    content.mkdir()
    for name in lab_backup.CONTENTS:
        path = content / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((tmp_path / name).read_bytes() if name.startswith("config/") else b"data")
    (content / "manifest.json").write_text(json.dumps({"format": 1, "historyCounts": [0] * 6}))
    (content / "releases.json").write_text("[]\n")
    with tarfile.open(content / "registry.tar", "w") as registry:
        data = b"blob"
        info = tarfile.TarInfo("./docker/registry/v2/blob")
        info.size = len(data)
        registry.addfile(info, io.BytesIO(data))
    (content / "SHA256SUMS").write_text(
        "".join(f"{lab_backup.sha256(content / name)}  {name}\n" for name in lab_backup.CONTENTS)
    )
    archive = tmp_path / "archive.tar.zst"
    zstd = importlib.import_module("compression.zstd")
    with archive.open("wb") as raw, zstd.open(raw, "wb") as compressed:
        with tarfile.open(fileobj=compressed, mode="w|") as tar:
            for name in (*lab_backup.CONTENTS, "SHA256SUMS"):
                tar.add(content / name, arcname=name)
    return archive


def test_verified_archive_accepts_complete_checksum_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = archive_fixture(tmp_path, monkeypatch)
    assert lab_backup.verify(archive)["format"] == 1


def test_tampered_archive_is_rejected_before_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = archive_fixture(tmp_path, monkeypatch)
    content = bytearray(archive.read_bytes())
    content[len(content) // 2] ^= 1
    archive.write_bytes(content)
    with pytest.raises((lab_backup.BackupError, OSError, tarfile.TarError)):
        lab_backup.verify(archive)


def test_concurrent_backup_is_rejected_before_quiescing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab_backup, "BACKUPS", tmp_path)
    with (tmp_path / ".workflow.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(lab_backup.BackupError, match="in progress"):
            lab_backup.backup()


@pytest.mark.parametrize("encoded", [False, True])
def test_known_credentials_are_rejected_from_backup_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoded: bool
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    secret_root = tmp_path / ".local/share/local-application-platform/secrets"
    secret_root.mkdir(parents=True)
    credential = b"fixture-only-credential-value"
    (secret_root / "token").write_bytes(credential)
    stage = tmp_path / "stage"
    for name in lab_backup.CONTENTS:
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ordinary content")
    (stage / "registry.tar").write_bytes(base64.b64encode(credential) if encoded else credential)
    with pytest.raises(lab_backup.BackupError, match="known credential bytes"):
        lab_backup.scan_known_secrets(stage)


def test_migration_finds_uv_in_user_local_bin(tmp_path: Path) -> None:
    home = tmp_path / "home"
    local_bin = home / ".local/bin"
    local_bin.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in {
        "uv": "#!/bin/sh\ntest \"$1 $2 $3\" = 'run --package platform-api'\n",
        "kubectl": "#!/bin/sh\nsleep 30\n",
        "nc": "#!/bin/sh\nexit 0\n",
    }.items():
        path = (local_bin if name == "uv" else bin_dir) / name
        path.write_text(script)
        path.chmod(0o755)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "postgresql-application-password").write_text("fixture-only-password")
    result = subprocess.run(  # noqa: S603 - all inputs are local test fixtures.
        [
            "/bin/sh",
            "tools/migrate-platform-api.sh",
            str(bin_dir / "kubectl"),
            "unused",
            str(secrets),
            "unused",
        ],
        env={**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
