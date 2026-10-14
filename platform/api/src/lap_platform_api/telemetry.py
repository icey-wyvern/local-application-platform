from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, cast

from fastapi import FastAPI
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


class _ShutdownProvider(Protocol):
    def shutdown(self) -> None: ...


@dataclass
class TelemetryRuntime:
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    logger_provider: LoggerProvider | None = None
    logging_handler: LoggingHandler | None = None
    request_counter: Counter | None = None
    request_duration: Histogram | None = None

    def record_request(self, method: str, route: str, status: int, duration: float) -> None:
        attributes = {"http.request.method": method, "http.route": route}
        if self.request_counter is not None:
            self.request_counter.add(1, {**attributes, "http.response.status_code": status})
        if self.request_duration is not None:
            self.request_duration.record(duration, attributes)

    def shutdown(self) -> None:
        if self.logging_handler is not None:
            logging.getLogger().removeHandler(self.logging_handler)
        if self.logger_provider is not None:
            cast("_ShutdownProvider", self.logger_provider).shutdown()
        if self.meter_provider is not None:
            self.meter_provider.shutdown()
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()


def configure_telemetry(app: FastAPI, endpoint: str | None) -> TelemetryRuntime:
    if endpoint is None:
        return TelemetryRuntime()
    base_endpoint = endpoint.rstrip("/")
    resource = Resource.create({
        "service.name": "platform-api",
        "service.namespace": "local-application-platform",
        "deployment.environment.name": "lab",
    })
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base_endpoint}/v1/traces"))
    )
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{base_endpoint}/v1/metrics")
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    meter = meter_provider.get_meter("lap-platform-api")
    request_counter = meter.create_counter("lap.platform_api.http.requests")
    request_duration = meter.create_histogram("lap.platform_api.http.request.duration", unit="s")
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base_endpoint}/v1/logs"))
    )
    handler = LoggingHandler(logger_provider=logger_provider)
    logging.getLogger().addHandler(handler)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
    return TelemetryRuntime(
        tracer_provider,
        meter_provider,
        logger_provider,
        handler,
        request_counter,
        request_duration,
    )
