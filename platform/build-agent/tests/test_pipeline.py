import io
import json
import os
import sys
import tarfile
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest
from lap_build_agent.config import Settings
from lap_build_agent.errors import BuildError
from lap_build_agent.models import BuildRequest
from lap_build_agent.pipeline import BuildPipeline, CommandRunner, exclusive_build_lock


def settings(tmp_path: Path) -> Settings:
    return Settings(
        listen_host="127.0.0.1",
        listen_port=8443,
        state_path=tmp_path,
        buildkit_socket=tmp_path / "buildkit.sock",
        trivy_cache_path=tmp_path / "cache",
        registry="registry.lab.test:5000",
        registry_namespace="apps",
        registry_auth_file=tmp_path / "auth.json",
        vulnerability_threshold="CRITICAL",
        build_timeout_seconds=900,
        otlp_endpoint=None,
    )


REQUEST = BuildRequest.model_validate({
    "buildId": UUID("00000000-0000-4000-8000-000000000003"),
    "repositoryUrl": "https://example.com/repo.git",
    "revision": {"type": "branch", "value": "main"},
})


class FakeRunner:
    def __init__(self, severities: list[str]) -> None:
        self.severities = severities
        self.commands: list[list[str]] = []
        self.published = False

    def run(
        self,
        argv: Sequence[str],
        *,
        stage: str,
        deadline: float,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        del deadline, cwd, env
        command = list(argv)
        self.commands.append(command)
        if command[:3] == ["git", "-C", command[2]] and "checkout" in command:
            source = Path(command[2])
            (source / "platform.yaml").write_text(
                "name: pipeline-test\nport: 8080\nhealthPath: /health\n"
            )
            (source / "Dockerfile").write_text("FROM scratch\n")
        if "rev-parse" in command:
            return "a" * 40
        if stage == "build":
            output = next(value for value in command if value.startswith("type=oci,dest="))
            archive = Path(output.split(",", 2)[1].removeprefix("dest="))
            with tarfile.open(archive, "w") as tar:
                data = b'{"imageLayoutVersion":"1.0.0"}'
                info = tarfile.TarInfo("oci-layout")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        if stage == "scan" and command[0] == "trivy":
            report = Path(command[command.index("--output") + 1])
            report.write_text(
                json.dumps({
                    "SchemaVersion": 2,
                    "Results": [
                        {
                            "Vulnerabilities": [
                                {"Severity": severity} for severity in self.severities
                            ]
                        }
                    ],
                })
            )
        if command[:2] == ["skopeo", "copy"]:
            self.published = True
        if command[:2] == ["skopeo", "--override-os"] or (
            command[:2] == ["skopeo", "inspect"] and self.published
        ):
            return "sha256:" + "b" * 64
        return ""


@pytest.mark.skipif(os.name != "posix", reason="pipeline uses the Linux flock boundary")
def test_successful_pipeline_publishes_the_scanned_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner([])
    monkeypatch.setattr(
        "lap_build_agent.pipeline.resolve_public_git_url",
        lambda _url: "https://example.com/repo.git",
    )
    (tmp_path / "workspaces").mkdir()

    result = BuildPipeline(settings(tmp_path), runner).execute(REQUEST)

    assert result.commit == "a" * 40
    assert result.imageDigest == "sha256:" + "b" * 64
    assert result.imageReference.endswith("@" + result.imageDigest)
    scan_index = next(i for i, command in enumerate(runner.commands) if command[0] == "trivy")
    publish_index = next(
        i for i, command in enumerate(runner.commands) if command[:2] == ["skopeo", "copy"]
    )
    assert scan_index < publish_index


@pytest.mark.skipif(os.name != "posix", reason="pipeline uses the Linux flock boundary")
def test_registry_outage_retries_publication_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecoveringRegistry(FakeRunner):
        def __init__(self) -> None:
            super().__init__([])
            self.copy_attempts = 0
            self.published = False

        def run(
            self,
            argv: Sequence[str],
            *,
            stage: str,
            deadline: float,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
        ) -> str:
            command = list(argv)
            if command[:2] == ["skopeo", "inspect"] and not self.published:
                raise BuildError(
                    "publish_failed", "registry unavailable", stage=stage, retryable=True
                )
            if command[:2] == ["skopeo", "copy"]:
                self.copy_attempts += 1
                if self.copy_attempts == 1:
                    raise BuildError(
                        "publish_failed", "registry unavailable", stage=stage, retryable=True
                    )
                self.published = True
            return super().run(argv, stage=stage, deadline=deadline, cwd=cwd, env=env)

    runner = RecoveringRegistry()
    monkeypatch.setattr(
        "lap_build_agent.pipeline.resolve_public_git_url",
        lambda _url: "https://example.com/repo.git",
    )
    monkeypatch.setattr("lap_build_agent.pipeline.time.sleep", lambda _seconds: None)
    (tmp_path / "workspaces").mkdir()

    result = BuildPipeline(settings(tmp_path), runner).execute(REQUEST)

    assert result.imageDigest == "sha256:" + "b" * 64
    assert runner.copy_attempts == 2
    assert len([command for command in runner.commands if command[0] == "buildctl"]) == 1


@pytest.mark.skipif(os.name != "posix", reason="pipeline uses the Linux flock boundary")
def test_policy_rejection_never_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner(["CRITICAL"])
    monkeypatch.setattr(
        "lap_build_agent.pipeline.resolve_public_git_url",
        lambda _url: "https://example.com/repo.git",
    )
    (tmp_path / "workspaces").mkdir()

    with pytest.raises(BuildError) as raised:
        BuildPipeline(settings(tmp_path), runner).execute(REQUEST)

    assert raised.value.code == "vulnerability_policy_rejected"
    assert not any(command[:2] == ["skopeo", "copy"] for command in runner.commands)


def test_command_timeout_is_not_reported_as_success() -> None:
    with pytest.raises(BuildError) as raised:
        CommandRunner().run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stage="build",
            deadline=time.monotonic() + 0.05,
        )
    assert raised.value.code == "build_timed_out"


@pytest.mark.skipif(os.name != "posix", reason="flock is a Linux service boundary")
def test_second_local_build_lock_is_rejected(tmp_path: Path) -> None:
    lock = tmp_path / "build.lock"
    with exclusive_build_lock(lock):
        with pytest.raises(BuildError) as raised, exclusive_build_lock(lock):
            pass
    assert raised.value.code == "build_in_progress"
