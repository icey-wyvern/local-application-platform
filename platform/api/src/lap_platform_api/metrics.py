from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class ApiMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            "lap_platform_api_http_requests_total",
            "Platform API HTTP requests",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.request_duration = Histogram(
            "lap_platform_api_http_request_duration_seconds",
            "Platform API HTTP request duration",
            ["method", "route"],
            registry=self.registry,
        )
        self.readiness = Counter(
            "lap_platform_api_readiness_checks_total",
            "Platform API readiness check results",
            ["result"],
            registry=self.registry,
        )
        self.scheduler_queue_depth = Gauge(
            "lap_platform_api_scheduler_queue_depth",
            "Queued durable operations",
            registry=self.registry,
        )
        self.scheduler_claims = Counter(
            "lap_platform_api_scheduler_claims_total",
            "Durable operation claims",
            ["operation_type", "claim_kind"],
            registry=self.registry,
        )
        self.scheduler_running = Gauge(
            "lap_platform_api_scheduler_running_operations",
            "Operations executing in this API process",
            ["operation_type"],
            registry=self.registry,
        )
        self.scheduler_outcomes = Counter(
            "lap_platform_api_scheduler_outcomes_total",
            "Operation execution outcomes",
            ["operation_type", "outcome"],
            registry=self.registry,
        )
        self.scheduler_lease_renewal_failures = Counter(
            "lap_platform_api_scheduler_lease_renewal_failures_total",
            "Failed operation lease renewals",
            ["operation_type"],
            registry=self.registry,
        )
        self.scheduler_execution_duration = Histogram(
            "lap_platform_api_scheduler_execution_duration_seconds",
            "Operation handler execution duration",
            ["operation_type", "outcome"],
            registry=self.registry,
        )
        self.scheduler_errors = Counter(
            "lap_platform_api_scheduler_errors_total",
            "Internal scheduler errors",
            ["stage"],
            registry=self.registry,
        )
        self.build_dispatches = Counter(
            "lap_platform_api_build_dispatches_total",
            "Build-agent dispatch attempts",
            ["result"],
            registry=self.registry,
        )
        self.build_agent_duration = Histogram(
            "lap_platform_api_build_agent_duration_seconds",
            "Build-agent request duration",
            ["result"],
            registry=self.registry,
        )
        self.build_results = Counter(
            "lap_platform_api_build_results_total",
            "Durable build outcomes",
            ["outcome", "failure_kind"],
            registry=self.registry,
        )
        self.release_creations = Counter(
            "lap_platform_api_release_creations_total",
            "Durable release creation results",
            ["result"],
            registry=self.registry,
        )
        self.application_cr_actions = Counter(
            "lap_platform_api_application_cr_actions_total",
            "Application custom-resource actions and bounded outcomes",
            ["result"],
            registry=self.registry,
        )
        self.application_observation_duration = Histogram(
            "lap_platform_api_application_observation_duration_seconds",
            "Time until the controller accepts an Application generation",
            ["result"],
            registry=self.registry,
        )
        self.deployment_acceptance_duration = Histogram(
            "lap_platform_api_deployment_acceptance_duration_seconds",
            "Time from Application apply until final deployment acceptance",
            ["outcome"],
            registry=self.registry,
        )
        self.deployment_acceptance_stages = Counter(
            "lap_platform_api_deployment_acceptance_stages_total",
            "Bounded deployment acceptance stage outcomes",
            ["stage", "outcome"],
            registry=self.registry,
        )
        self.rollbacks = Counter(
            "lap_platform_api_rollbacks_total",
            "Automatic rollback lifecycle outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self.rollback_duration = Histogram(
            "lap_platform_api_rollback_duration_seconds",
            "Automatic rollback acceptance duration",
            ["outcome"],
            registry=self.registry,
        )
        self.rollback_acceptance_stages = Counter(
            "lap_platform_api_rollback_acceptance_stages_total",
            "Automatic rollback acceptance stage outcomes",
            ["stage", "outcome"],
            registry=self.registry,
        )
