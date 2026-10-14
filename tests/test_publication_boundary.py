"""The release scanner must inspect the exact Git publication candidates."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess  # noqa: S404 - fixed Git commands run only inside a disposable test repository.
from pathlib import Path
from types import ModuleType

import pytest


def _scanner() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools/scan-secret-exposure.py"
    spec = importlib.util.spec_from_file_location("lap_secret_scan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_candidates_include_hidden_and_forced_staged_files(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - fixed Git command and pytest-owned temporary path.
        [git, "init", "-q", str(tmp_path)], check=True
    )
    (tmp_path / ".gitignore").write_text("private/\n", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.key").write_text("fixture-only\n", encoding="utf-8")
    scanner = _scanner()
    scanner.__dict__["ROOT"] = tmp_path
    assert set(scanner.public_candidate_names()) == {".gitignore", ".github/ci.yml"}
    subprocess.run(  # noqa: S603 - fixed fixture file in a disposable test repository.
        [git, "add", "-f", "private/secret.key"], cwd=tmp_path, check=True
    )
    assert set(scanner.public_candidate_names()) == {
        ".gitignore",
        ".github/ci.yml",
        "private/secret.key",
    }


def test_public_boundary_rejects_private_paths_and_credentials(tmp_path: Path) -> None:
    scanner = _scanner()
    (tmp_path / "README.md").write_text("A public project\n", encoding="utf-8")
    scanner.check_public_boundary(["README.md"], tmp_path)

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lab.yaml").write_text("fixture\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="private path"):
        scanner.check_public_boundary(["config/lab.yaml"], tmp_path)

    (tmp_path / "README.md").write_text(
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nfixture\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="credential pattern"):
        scanner.check_public_boundary(["README.md"], tmp_path)
