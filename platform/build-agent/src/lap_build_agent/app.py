import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from typing import Protocol
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from lap_build_agent.config import Settings
from lap_build_agent.errors import BuildError
from lap_build_agent.models import BuildRequest, BuildResult, ErrorDetail, ErrorResponse
from lap_build_agent.pipeline import BuildPipeline

LOGGER = logging.getLogger(__name__)
BUILD_TOTAL = Counter("lap_build_agent_builds_total", "Build results", ["result"])
STAGE_SECONDS = Histogram("lap_build_agent_build_duration_seconds", "Total build duration")


class Pipeline(Protocol):
    def execute(self, request: BuildRequest) -> BuildResult: ...


class BuildCoordinator:
    """Ephemeral idempotency only; PostgreSQL will remain authoritative."""

    def __init__(
        self, pipeline_factory: Callable[[], Pipeline], maximum_results: int = 128
    ) -> None:
        self._pipeline_factory = pipeline_factory
        self._maximum_results = maximum_results
        self._lock = Lock()
        self._running: dict[UUID, str] = {}
        self._completed: OrderedDict[UUID, tuple[str, BuildResult | BuildError]] = OrderedDict()

    def execute(self, request: BuildRequest) -> BuildResult:
        fingerprint = request.model_dump_json()
        with self._lock:
            completed = self._completed.get(request.buildId)
            if completed:
                previous_fingerprint, cached_outcome = completed
                if previous_fingerprint != fingerprint:
                    raise self._conflict()
                self._completed.move_to_end(request.buildId)
                if isinstance(cached_outcome, BuildError):
                    if not cached_outcome.retryable:
                        raise cached_outcome
                else:
                    return cached_outcome
            running_fingerprint = self._running.get(request.buildId)
            if running_fingerprint is not None:
                if running_fingerprint != fingerprint:
                    raise self._conflict()
                raise BuildError(
                    "build_already_running",
                    "this build identifier is already running",
                    status_code=409,
                    retryable=True,
                )
            self._running[request.buildId] = fingerprint
        try:
            executed_outcome: BuildResult | BuildError = self._pipeline_factory().execute(request)
        except BuildError as error:
            executed_outcome = error
        except Exception:
            LOGGER.exception("unexpected build failure", extra={"build_id": str(request.buildId)})
            executed_outcome = BuildError(
                "internal_error",
                "the build agent encountered an unexpected error",
                status_code=500,
                retryable=True,
            )
        with self._lock:
            self._running.pop(request.buildId, None)
            self._completed[request.buildId] = (fingerprint, executed_outcome)
            while len(self._completed) > self._maximum_results:
                self._completed.popitem(last=False)
        if isinstance(executed_outcome, BuildError):
            raise executed_outcome
        return executed_outcome

    @staticmethod
    def _conflict() -> BuildError:
        return BuildError(
            "build_id_conflict",
            "build identifier was already used with a different request",
            status_code=409,
        )


def create_app(
    settings: Settings | None = None,
    pipeline_factory: Callable[[], Pipeline] | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    factory = pipeline_factory or (lambda: BuildPipeline(resolved_settings))
    coordinator = BuildCoordinator(factory)
    app = FastAPI(
        title="Local Application Platform Build Agent",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(BuildError)
    async def build_error_handler(_request: Request, error: BuildError) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code=error.code,
                message=error.message,
                stage=error.stage,
                retryable=error.retryable,
            )
        )
        return JSONResponse(status_code=error.status_code, content=body.model_dump(mode="json"))

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post(
        "/v1/builds",
        response_model=BuildResult,
        responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    )
    def create_build(request: BuildRequest) -> BuildResult:
        started = time.monotonic()
        try:
            result = coordinator.execute(request)
        except BuildError:
            BUILD_TOTAL.labels(result="failed").inc()
            raise
        BUILD_TOTAL.labels(result="succeeded").inc()
        STAGE_SECONDS.observe(time.monotonic() - started)
        return result

    FastAPIInstrumentor.instrument_app(app)
    return app
