from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from lap_platform_api.metrics import ApiMetrics
from lap_platform_api.telemetry import TelemetryRuntime
from opentelemetry.metrics import Counter, Histogram
from prometheus_client import generate_latest


@pytest.mark.unit
def test_request_metrics_have_bounded_route_attributes() -> None:
    counter = MagicMock(spec=Counter)
    histogram = MagicMock(spec=Histogram)
    runtime = TelemetryRuntime(request_counter=counter, request_duration=histogram)

    runtime.record_request("GET", "/api/v1/info", 200, 0.25)

    counter.add.assert_called_once_with(
        1,
        {
            "http.request.method": "GET",
            "http.route": "/api/v1/info",
            "http.response.status_code": 200,
        },
    )
    histogram.record.assert_called_once_with(
        0.25,
        {"http.request.method": "GET", "http.route": "/api/v1/info"},
    )


@pytest.mark.unit
def test_scheduler_metrics_use_only_bounded_labels() -> None:
    metrics = ApiMetrics()
    metrics.scheduler_queue_depth.set(3)
    metrics.scheduler_claims.labels("deploy", "reclaimed").inc()
    metrics.scheduler_running.labels("deploy").set(2)
    metrics.scheduler_outcomes.labels("deploy", "succeeded").inc()
    metrics.scheduler_lease_renewal_failures.labels("deploy").inc()
    metrics.scheduler_execution_duration.labels("deploy", "succeeded").observe(0.25)

    exposition = generate_latest(metrics.registry).decode()

    assert "lap_platform_api_scheduler_queue_depth 3.0" in exposition
    assert 'operation_type="deploy"' in exposition
    assert 'claim_kind="reclaimed"' in exposition
    assert "operation_id" not in exposition
