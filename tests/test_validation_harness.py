from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess  # noqa: S404 - subprocess exercises the harness as an external command.
import sys
import tempfile
import time
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "tools" / "validation_harness.py"


def run_harness(
    *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed Python executable and test arguments.
        [sys.executable, str(HARNESS), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def artifact_from(result: subprocess.CompletedProcess[str]) -> Path:
    match = re.search(r"Diagnostics: (.+)", result.stderr)
    assert match is not None, result.stderr
    artifact = Path(match.group(1))
    assert artifact.is_dir()
    return artifact


def test_success_is_compact_and_removes_diagnostics() -> None:
    before = set(Path(tempfile.gettempdir()).glob("lap-validation-*"))
    for _ in range(2):
        result = run_harness(
            "--stage",
            "compact success",
            "--",
            sys.executable,
            "-c",
            "print('verbose child output')",
        )
        assert result.returncode == 0
        assert result.stdout == "PASS compact success\n"
        assert result.stderr == ""
    assert set(Path(tempfile.gettempdir()).glob("lap-validation-*")) == before


def test_failure_preserves_machine_result_and_redacts_secret_output() -> None:
    environment = os.environ.copy()
    environment["VALIDATION_TEST_" + "PASSWORD"] = "test-secret-value"  # noqa: S105
    result = run_harness(
        "--stage",
        "intentional failure",
        "--",
        sys.executable,
        "-c",
        "import os,sys; "
        "print('password=' + os.environ['VALIDATION_TEST_PASSWORD']); "
        "print('failed details', file=sys.stderr); sys.exit(7)",
        env=environment,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "FAIL intentional failure" in result.stderr
    artifact = artifact_from(result)
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o700
    assert stat.S_IMODE((artifact / "stdout.log").stat().st_mode) == 0o600
    assert "test-secret-value" not in (artifact / "stdout.log").read_text()
    assert "<redacted>" in (artifact / "stdout.log").read_text()
    assert "failed details" in (artifact / "stderr.log").read_text()
    data = json.loads((artifact / "result.json").read_text())
    assert data["stage"] == "intentional failure"
    assert data["expected"] == "exit 0"
    assert data["actual"] == "exit 7"
    assert data["outcome"] == "failed"
    assert data["failure_kind"] == "command_failure"


def test_failure_classifies_assertions_and_infrastructure() -> None:
    for diagnostic, expected in (
        ("AssertionError: expected ready replicas", "assertion"),
        ("kubectl: connection refused", "infrastructure"),
    ):
        result = run_harness(
            "--stage",
            "classified failure",
            "--",
            sys.executable,
            "-c",
            f"import sys; print({diagnostic!r}, file=sys.stderr); sys.exit(1)",
        )
        assert result.returncode == 1
        artifact = artifact_from(result)
        data = json.loads((artifact / "result.json").read_text())
        assert data["failure_kind"] == expected


def test_timeout_is_bounded_and_preserves_artifact() -> None:
    started = time.monotonic()
    result = run_harness(
        "--stage",
        "bounded timeout",
        "--timeout",
        "0.2",
        "--",
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    )
    assert result.returncode == 1
    assert time.monotonic() - started < 8
    artifact = artifact_from(result)
    data = json.loads((artifact / "result.json").read_text())
    assert data["timed_out"] is True
    assert data["actual"] == "timeout after 0.2s"


def test_failure_diagnostics_are_scoped_and_never_read_secrets(tmp_path: Path) -> None:
    fake_kubectl = tmp_path / "kubectl"
    fake_kubectl.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "assert 'secrets' not in args\n"
        "print('snapshot: ' + ' '.join(args))\n",
        encoding="utf-8",
    )
    fake_kubectl.chmod(0o700)
    result = run_harness(
        "--stage",
        "API readiness",
        "--kubectl",
        str(fake_kubectl),
        "--kubeconfig",
        "test-kubeconfig",
        "--namespace",
        "platform-system",
        "--deployment",
        "platform-api",
        "--selector",
        "app.kubernetes.io/name=platform-api",
        "--",
        sys.executable,
        "-c",
        "raise SystemExit(3)",
    )
    assert result.returncode == 1
    artifact = artifact_from(result)
    kubernetes = artifact / "kubernetes"
    assert "platform-api" in (kubernetes / "deployment.yaml").read_text()
    assert "platform-system" in (kubernetes / "pods.yaml").read_text()
    assert "events" in (kubernetes / "warning-events.txt").read_text()


def test_interrupt_stops_child_and_preserves_artifact() -> None:
    if os.name == "nt":
        return
    process = subprocess.Popen(  # noqa: S603 - fixed Python command invokes the harness.
        [
            sys.executable,
            str(HARNESS),
            "--stage",
            "interrupt cleanup",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    time.sleep(0.2)
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=18)
    assert process.returncode == 1
    assert stdout == ""
    artifact = artifact_from(subprocess.CompletedProcess([], 1, stdout, stderr))
    data = json.loads((artifact / "result.json").read_text())
    assert data["outcome"] == "interrupted"
