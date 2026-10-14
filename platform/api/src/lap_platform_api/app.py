from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from lap_platform_api.api_routes import create_public_router
from lap_platform_api.auth import BearerAuthenticationDependency, BearerTokenAuthenticator
from lap_platform_api.build_agent import BuildAgentClient
from lap_platform_api.build_dispatch import DeployBuildHandler, ManualRollbackHandler
from lap_platform_api.config import ApiSettings
from lap_platform_api.database import create_database_engine, create_session_factory
from lap_platform_api.delete_handler import DeleteApplicationHandler
from lap_platform_api.errors import PlatformError, ProblemDetail, problem_type
from lap_platform_api.health import (
    DependencyProbe,
    KubernetesProbe,
    PostgreSQLProbe,
    ReadinessChecker,
)
from lap_platform_api.kubernetes import ApplicationClient
from lap_platform_api.logging import reset_request_id, set_request_id
from lap_platform_api.metrics import ApiMetrics
from lap_platform_api.scheduler import OperationHandler, OperationScheduler
from lap_platform_api.schemas import ApiInfoResponse, HealthResponse
from lap_platform_api.states import OperationType
from lap_platform_api.telemetry import TelemetryRuntime, configure_telemetry

LOGGER = logging.getLogger(__name__)
REQUEST_ID_HEADER = "X-Request-ID"


@dataclass
class AppDependencies:
    engine: Engine
    session_factory: sessionmaker[Session]
    readiness: ReadinessChecker
    build_agent: BuildAgentClient
    application_client: ApplicationClient | None = None
    owns_resources: bool = False

    async def close(self) -> None:
        if self.owns_resources:
            if self.application_client is not None:
                await self.application_client.close()
            await self.build_agent.close()
            self.engine.dispose()


def create_dependencies(settings: ApiSettings) -> AppDependencies:
    engine = create_database_engine(settings.database)
    probes: list[DependencyProbe] = [PostgreSQLProbe(engine)]
    if settings.kubernetes.enabled:
        probes.append(KubernetesProbe(settings.kubernetes))
    return AppDependencies(
        engine=engine,
        session_factory=create_session_factory(engine),
        readiness=ReadinessChecker(probes, settings.readiness_cache_seconds),
        build_agent=BuildAgentClient(settings.build_agent),
        application_client=(
            ApplicationClient(
                settings.kubernetes,
                health_ca_certificate=settings.build_agent.ca_certificate,
            )
            if settings.kubernetes.enabled
            else None
        ),
        owns_resources=True,
    )


def _correlation_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", uuid4()))


def _problem_response(
    request: Request,
    *,
    status_code: int,
    error_code: str,
    detail: str,
    title: str | None = None,
    headers: dict[str, str] | None = None,
    extensions: dict[str, object] | None = None,
) -> JSONResponse:
    correlation_id = _correlation_id(request)
    problem = ProblemDetail(
        type=problem_type(error_code),
        title=title or HTTPStatus(status_code).phrase,
        status=status_code,
        detail=detail,
        instance=request.url.path,
        errorCode=error_code,
        correlationId=correlation_id,
        extensions=extensions,
    )
    response_headers = {REQUEST_ID_HEADER: correlation_id, **(headers or {})}
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json", by_alias=True, exclude_none=True),
        headers=response_headers,
        media_type="application/problem+json",
    )


def create_app(
    settings: ApiSettings | None = None,
    dependencies: AppDependencies | None = None,
) -> FastAPI:
    resolved_settings = settings or ApiSettings.from_environment()
    resolved_dependencies = dependencies or create_dependencies(resolved_settings)
    authenticator = BearerTokenAuthenticator(resolved_settings.token_sha256)
    authentication_dependency = BearerAuthenticationDependency(authenticator)
    metrics = ApiMetrics()
    telemetry = TelemetryRuntime()
    scheduler: OperationScheduler | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            if scheduler is not None:
                await scheduler.start()
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            telemetry.shutdown()
            await resolved_dependencies.close()

    app = FastAPI(
        title="Local Application Platform API",
        version=resolved_settings.service_version,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.dependencies = resolved_dependencies
    app.state.metrics = metrics

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get(REQUEST_ID_HEADER)
        try:
            request_id = str(UUID(supplied, version=4)) if supplied is not None else str(uuid4())
            if supplied is not None and request_id != supplied.casefold():
                raise ValueError
        except ValueError:
            request.state.correlation_id = str(uuid4())
            return _problem_response(
                request,
                status_code=400,
                error_code="REQUEST_ID_INVALID",
                detail="X-Request-ID must be a canonical UUID version 4",
            )
        request.state.correlation_id = request_id
        token = set_request_id(request_id)
        started = time.monotonic()
        span = trace.get_current_span()
        span.set_attribute("lap.request.id", request_id)
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            route = getattr(request.scope.get("route"), "path", "unmatched")
            elapsed = time.monotonic() - started
            metrics.requests.labels(request.method, route, str(response.status_code)).inc()
            metrics.request_duration.labels(request.method, route).observe(elapsed)
            telemetry.record_request(request.method, route, response.status_code, elapsed)
            LOGGER.info(
                "request completed",
                extra={
                    "method": request.method,
                    "path": route,
                    "status_code": response.status_code,
                    "duration_ms": round(elapsed * 1000, 3),
                },
            )
            return response
        finally:
            reset_request_id(token)

    @app.exception_handler(PlatformError)
    async def platform_error_handler(request: Request, error: PlatformError) -> JSONResponse:
        return _problem_response(
            request,
            status_code=error.status_code,
            error_code=error.error_code,
            detail=error.detail,
            title=error.title,
            headers=error.headers,
            extensions=error.extensions,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return _problem_response(
            request,
            status_code=422,
            error_code="REQUEST_VALIDATION_FAILED",
            detail="the request did not satisfy the API contract",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
        error_code = "RESOURCE_NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR"
        return _problem_response(
            request,
            status_code=error.status_code,
            error_code=error_code,
            detail=str(error.detail),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
        LOGGER.exception("unexpected request failure", exc_info=error)
        return _problem_response(
            request,
            status_code=500,
            error_code="INTERNAL_ERROR",
            detail="the platform API encountered an unexpected error",
        )

    @app.get("/healthz", response_model=HealthResponse, include_in_schema=False)
    def liveness() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/readyz", response_model=HealthResponse, include_in_schema=False)
    def readiness() -> HealthResponse:
        result = resolved_dependencies.readiness.check()
        metrics.readiness.labels("ready" if result.ready else "not_ready").inc()
        if not result.ready:
            raise PlatformError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "one or more required dependencies are unavailable",
                extensions={"dependencies": list(result.failures)},
            )
        return HealthResponse(status="ready")

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(
            generate_latest(metrics.registry),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(authentication_dependency)])

    @router.get("/info", response_model=ApiInfoResponse)
    def api_info() -> ApiInfoResponse:
        return ApiInfoResponse(
            service="platform-api", apiVersion="v1", version=resolved_settings.service_version
        )

    app.include_router(router)
    app.include_router(
        create_public_router(
            authentication_dependency=authentication_dependency,
            database_dependency=database_session,
        )
    )
    telemetry = configure_telemetry(app, resolved_settings.otlp_endpoint)
    if resolved_settings.scheduler.enabled:
        if resolved_dependencies.application_client is None:
            raise ValueError("scheduler requires the Kubernetes Application client")
        scheduler_tracer = (
            telemetry.tracer_provider.get_tracer("lap-platform-api.scheduler")
            if telemetry.tracer_provider is not None
            else None
        )
        build_dispatch_tracer = (
            telemetry.tracer_provider.get_tracer("lap-platform-api.build-dispatch")
            if telemetry.tracer_provider is not None
            else None
        )
        operation_handlers: dict[str, OperationHandler] = {
            OperationType.DEPLOY.value: DeployBuildHandler(
                resolved_dependencies.session_factory,
                resolved_dependencies.build_agent,
                metrics,
                application_client=resolved_dependencies.application_client,
                deployment_timeout_seconds=(
                    resolved_settings.kubernetes.deployment_timeout_seconds
                ),
                tracer=build_dispatch_tracer,
            ),
            OperationType.DELETE.value: DeleteApplicationHandler(
                resolved_dependencies.session_factory,
                resolved_dependencies.application_client,
            ),
            OperationType.ROLLBACK.value: ManualRollbackHandler(
                resolved_dependencies.session_factory,
                resolved_dependencies.build_agent,
                metrics,
                application_client=resolved_dependencies.application_client,
                deployment_timeout_seconds=(
                    resolved_settings.kubernetes.deployment_timeout_seconds
                ),
                tracer=build_dispatch_tracer,
            ),
        }
        scheduler = OperationScheduler(
            resolved_dependencies.session_factory,
            operation_handlers,
            metrics,
            poll_interval_seconds=resolved_settings.scheduler.poll_interval_seconds,
            retry_delay_seconds=resolved_settings.scheduler.retry_delay_seconds,
            tracer=scheduler_tracer,
        )
    app.state.scheduler = scheduler
    return app


def database_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.dependencies.session_factory
    with factory() as session:
        yield session
