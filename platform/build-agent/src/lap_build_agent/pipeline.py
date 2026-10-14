import hashlib
import importlib
import logging
import os
import re
import shutil
import subprocess  # noqa: S404 - commands use fixed argv without a shell.
import tarfile
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from uuid import UUID

from opentelemetry import metrics, trace
from prometheus_client import Counter, Histogram

from lap_build_agent.config import Settings
from lap_build_agent.errors import BuildError
from lap_build_agent.models import BuildRequest, BuildResult, RevisionType
from lap_build_agent.policy import evaluate_trivy_report
from lap_build_agent.validation import resolve_public_git_url, validate_platform_yaml

LOGGER = logging.getLogger(__name__)
TRACER = trace.get_tracer(__name__)
METER = metrics.get_meter(__name__)
OTEL_STAGE_TOTAL = METER.create_counter("lap.build_agent.stages", unit="{stage}")
OTEL_STAGE_DURATION = METER.create_histogram("lap.build_agent.stage.duration", unit="s")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
WORKSPACE_PATTERN = re.compile(
    r"^build-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-.+$"
)
FCNTL = importlib.import_module("fcntl") if os.name == "posix" else None
STAGES_TOTAL = Counter("lap_build_agent_stages_total", "Build stage results", ["stage", "result"])
STAGE_DURATION = Histogram(
    "lap_build_agent_stage_duration_seconds", "Build stage duration", ["stage"]
)


def cleanup_stale_workspaces(state_path: Path) -> None:
    workspaces = state_path / "workspaces"
    workspaces.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_root = workspaces.resolve()
    for candidate in workspaces.iterdir():
        if not WORKSPACE_PATTERN.fullmatch(candidate.name) or not candidate.is_dir():
            continue
        resolved_candidate = candidate.resolve()
        if resolved_root not in resolved_candidate.parents:
            continue
        shutil.rmtree(resolved_candidate)


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        stage: str,
        deadline: float,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise BuildError("build_timed_out", "build exceeded its time limit", stage=stage)
        try:
            process = subprocess.Popen(  # noqa: S603 - argv is fixed by the pipeline, never a shell.
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            if os.name == "posix":
                os.kill(-process.pid, 9)
            else:
                process.kill()
            process.wait()
            raise BuildError(
                "build_timed_out", "build exceeded its time limit", stage=stage
            ) from error
        except OSError as error:
            raise BuildError(
                "tool_execution_failed", f"could not execute {argv[0]}", stage=stage, retryable=True
            ) from error
        if process.returncode != 0:
            raise BuildError(
                f"{stage}_failed",
                f"{argv[0]} exited with status {process.returncode}",
                stage=stage,
                retryable=stage in {"clone", "publish"},
            )
        return stdout.strip()


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stage: str,
        deadline: float,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str: ...


@contextmanager
def exclusive_build_lock(path: Path) -> Iterator[None]:
    if FCNTL is None:
        raise RuntimeError("the build lock requires a POSIX host")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            FCNTL.flock(handle, FCNTL.LOCK_EX | FCNTL.LOCK_NB)
        except BlockingIOError as error:
            raise BuildError(
                "build_in_progress",
                "the build host is already executing a build",
                status_code=409,
                retryable=True,
            ) from error
        try:
            yield
        finally:
            FCNTL.flock(handle, FCNTL.LOCK_UN)


class BuildPipeline:
    def __init__(self, settings: Settings, runner: Runner | None = None) -> None:
        self.settings = settings
        self.runner = runner or CommandRunner()

    def execute(self, request: BuildRequest) -> BuildResult:
        deadline = time.monotonic() + self.settings.build_timeout_seconds
        build_id = str(request.buildId)
        with exclusive_build_lock(self.settings.state_path / "build.lock"):
            LOGGER.info("build started", extra={"build_id": build_id, "result": "started"})
            try:
                with tempfile.TemporaryDirectory(
                    prefix=f"build-{build_id}-", dir=self.settings.state_path / "workspaces"
                ) as temporary:
                    return self._execute(request, Path(temporary), deadline)
            except BuildError as error:
                LOGGER.warning(
                    "build failed",
                    extra={
                        "build_id": build_id,
                        "stage": error.stage,
                        "result": "failed",
                        "error_code": error.code,
                    },
                )
                raise

    def _execute(self, request: BuildRequest, workspace: Path, deadline: float) -> BuildResult:
        source = workspace / "source"
        archive = workspace / "image.oci.tar"
        scan_layout = workspace / "scan-layout"
        report = workspace / "trivy.json"

        with self._stage(request.buildId, "clone"):
            repository_url = resolve_public_git_url(request.repositoryUrl)
            source.mkdir(mode=0o700)
            git_env = {
                **os.environ,
                "HOME": str(workspace),
                "XDG_CONFIG_HOME": str(workspace / ".config"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "/bin/false",
                "GIT_SSH_COMMAND": "/bin/false",
            }
            self.runner.run(
                ["git", "init", "--quiet", str(source)],
                stage="clone",
                deadline=deadline,
                env=git_env,
            )
            self.runner.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "protocol.allow=never",
                    "-c",
                    "protocol.https.allow=always",
                    "-c",
                    "http.followRedirects=false",
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    repository_url,
                    self._revision_ref(request),
                ],
                stage="clone",
                deadline=deadline,
                env=git_env,
            )
            commit = self.runner.run(
                ["git", "-C", str(source), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
                stage="clone",
                deadline=deadline,
                env=git_env,
            )
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise BuildError(
                    "revision_resolution_failed", "Git returned an invalid commit", stage="clone"
                )
            self.runner.run(
                ["git", "-C", str(source), "checkout", "--quiet", "--detach", commit],
                stage="clone",
                deadline=deadline,
                env=git_env,
            )
            if (source / ".gitmodules").exists():
                raise BuildError(
                    "git_submodules_forbidden",
                    "repositories with Git submodules are not supported",
                    stage="clone",
                )

        with self._stage(request.buildId, "validate"):
            application = validate_platform_yaml(source / "platform.yaml")
            if not (source / "Dockerfile").is_file():
                raise BuildError(
                    "dockerfile_missing", "repository does not contain Dockerfile", stage="validate"
                )

        tag = f"build-{str(request.buildId).lower()}"
        target = (
            f"{self.settings.registry}/{self.settings.registry_namespace}/{application.name}:{tag}"
        )
        with self._stage(request.buildId, "build"):
            self.runner.run(
                [
                    "buildctl",
                    "--addr",
                    f"unix://{self.settings.buildkit_socket}",
                    "build",
                    "--frontend",
                    "dockerfile.v0",
                    "--local",
                    f"context={source}",
                    "--local",
                    f"dockerfile={source}",
                    "--output",
                    f"type=oci,dest={archive},name={target}",
                ],
                stage="build",
                deadline=deadline,
            )

        artifact_hash = self._sha256(archive)
        with self._stage(request.buildId, "scan"):
            scan_layout.mkdir(mode=0o700)
            self._extract_oci_archive(archive, scan_layout)
            self.runner.run(
                [
                    "trivy",
                    "image",
                    "--input",
                    str(scan_layout),
                    "--cache-dir",
                    str(self.settings.trivy_cache_path),
                    "--format",
                    "json",
                    "--output",
                    str(report),
                    "--scanners",
                    "vuln",
                    "--quiet",
                ],
                stage="scan",
                deadline=deadline,
            )
            vulnerabilities = evaluate_trivy_report(report, self.settings.vulnerability_threshold)
            if self._sha256(archive) != artifact_hash:
                raise BuildError(
                    "artifact_changed", "OCI artifact changed during scanning", stage="scan"
                )

        with self._stage(request.buildId, "publish"):
            source_digest = self.runner.run(
                [
                    "skopeo",
                    "--override-os",
                    "linux",
                    "inspect",
                    "--format",
                    "{{.Digest}}",
                    f"oci-archive:{archive}",
                ],
                stage="publish",
                deadline=deadline,
            )
            if not DIGEST_PATTERN.fullmatch(source_digest):
                raise BuildError(
                    "artifact_digest_invalid", "Skopeo returned an invalid digest", stage="publish"
                )
            copy_command = [
                "skopeo",
                "copy",
                "--authfile",
                str(self.settings.registry_auth_file),
                "--preserve-digests",
                f"oci-archive:{archive}",
                f"docker://{target}",
            ]
            inspect_command = [
                "skopeo",
                "inspect",
                "--authfile",
                str(self.settings.registry_auth_file),
                "--format",
                "{{.Digest}}",
                f"docker://{target}",
            ]
            while True:
                try:
                    existing_digest = self.runner.run(
                        inspect_command, stage="publish", deadline=deadline
                    )
                except BuildError:
                    existing_digest = None
                if existing_digest == source_digest:
                    break
                try:
                    self.runner.run(copy_command, stage="publish", deadline=deadline)
                    break
                except BuildError as error:
                    if not error.retryable:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 2:
                        raise BuildError(
                            "publish_unavailable",
                            "registry remained unavailable until the build deadline",
                            stage="publish",
                        ) from error
                    time.sleep(min(2, remaining))

        with self._stage(request.buildId, "verify"):
            published_digest = self.runner.run(
                [
                    "skopeo",
                    "inspect",
                    "--authfile",
                    str(self.settings.registry_auth_file),
                    "--format",
                    "{{.Digest}}",
                    f"docker://{target}",
                ],
                stage="verify",
                deadline=deadline,
            )
            if source_digest != published_digest:
                raise BuildError(
                    "registry_digest_mismatch",
                    "published registry digest does not match the scanned artifact",
                    stage="verify",
                )

        result = BuildResult(
            buildId=request.buildId,
            commit=commit,
            imageReference=f"{self.settings.registry}/{self.settings.registry_namespace}/{application.name}@{published_digest}",
            imageDigest=published_digest,
            application=application,
            vulnerabilities=vulnerabilities,
        )
        LOGGER.info(
            "build succeeded",
            extra={"build_id": str(request.buildId), "result": "succeeded"},
        )
        return result

    @staticmethod
    def _revision_ref(request: BuildRequest) -> str:
        if request.revision.type is RevisionType.BRANCH:
            return f"refs/heads/{request.revision.value}"
        if request.revision.type is RevisionType.TAG:
            return f"refs/tags/{request.revision.value}"
        if not re.fullmatch(r"[0-9a-fA-F]{40}", request.revision.value):
            raise BuildError(
                "revision_invalid",
                "commit revisions must be full 40-character hashes",
                stage="clone",
            )
        return request.revision.value.lower()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _extract_oci_archive(archive: Path, destination: Path) -> None:
        try:
            with tarfile.open(archive, mode="r:") as tar:
                for member in tar.getmembers():
                    target = (destination / member.name).resolve()
                    if (
                        destination.resolve() not in target.parents
                        and target != destination.resolve()
                    ):
                        raise BuildError(
                            "artifact_invalid", "OCI archive contains an unsafe path", stage="scan"
                        )
                    if not (member.isfile() or member.isdir()):
                        raise BuildError(
                            "artifact_invalid", "OCI archive contains an unsafe entry", stage="scan"
                        )
                tar.extractall(destination, filter="data")
        except (tarfile.TarError, OSError) as error:
            raise BuildError(
                "artifact_invalid", "BuildKit produced an invalid OCI archive", stage="scan"
            ) from error

    @contextmanager
    def _stage(self, build_id: UUID, stage: str) -> Iterator[None]:
        started = time.monotonic()
        LOGGER.info(
            "stage started", extra={"build_id": str(build_id), "stage": stage, "result": "started"}
        )
        with TRACER.start_as_current_span(f"build.{stage}") as span:
            span.set_attribute("build.id", str(build_id))
            span.set_attribute("build.stage", stage)
            try:
                yield
            except Exception:
                span.set_attribute("build.result", "failed")
                STAGES_TOTAL.labels(stage=stage, result="failed").inc()
                OTEL_STAGE_TOTAL.add(1, {"stage": stage, "result": "failed"})
                raise
            else:
                span.set_attribute("build.result", "succeeded")
                STAGES_TOTAL.labels(stage=stage, result="succeeded").inc()
                OTEL_STAGE_TOTAL.add(1, {"stage": stage, "result": "succeeded"})
                LOGGER.info(
                    "stage completed",
                    extra={"build_id": str(build_id), "stage": stage, "result": "succeeded"},
                )
            finally:
                duration = time.monotonic() - started
                STAGE_DURATION.labels(stage=stage).observe(duration)
                OTEL_STAGE_DURATION.record(duration, {"stage": stage})
