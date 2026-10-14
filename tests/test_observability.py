from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY = REPO_ROOT / "cluster" / "observability"


def load(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.mark.unit
def test_grafana_uses_gateway_tls_and_clusterip_backend() -> None:
    certificate = load(OBSERVABILITY / "grafana-certificate.yaml")
    assert certificate["metadata"]["namespace"] == "envoy-gateway-system"
    assert certificate["spec"]["dnsNames"] == ["grafana.lab.test"]
    assert certificate["spec"]["issuerRef"]["name"] == "lap-cluster-intermediate"
    routes = list(
        yaml.safe_load_all((OBSERVABILITY / "grafana-routes.yaml").read_text(encoding="utf-8"))
    )
    assert {route["metadata"]["name"] for route in routes} == {
        "grafana",
        "grafana-http-to-https",
    }
    https_route = next(route for route in routes if route["metadata"]["name"] == "grafana")
    assert https_route["spec"]["hostnames"] == ["grafana.lab.test"]
    assert https_route["spec"]["rules"][0]["backendRefs"] == [
        {"name": "kube-prometheus-stack-grafana", "port": 80}
    ]


@pytest.mark.unit
def test_initial_rules_cover_required_available_signals() -> None:
    rules = load(OBSERVABILITY / "prometheus-rules.yaml")["spec"]["groups"][0]["rules"]
    names = {rule["alert"] for rule in rules}
    assert names == {
        "KubernetesNodeNotReady",
        "KubernetesAPIRedundancyLost",
        "ExcessivePodRestarts",
        "PrometheusTargetDown",
        "LonghornVolumeUnhealthy",
        "PostgreSQLUnavailable",
        "PersistentVolumeCapacityCritical",
    }


@pytest.mark.unit
def test_dashboards_are_small_version_controlled_json() -> None:
    expected = {
        "cluster-health.json": ("lap-cluster-health", "Cluster Health"),
        "platform-health.json": ("lap-platform-health", "Platform Health"),
        "telemetry-pipelines.json": ("lap-telemetry-pipelines", "Telemetry Pipelines"),
    }
    for filename, (uid, title) in expected.items():
        dashboard = json.loads((OBSERVABILITY / "dashboards" / filename).read_text())
        assert dashboard["uid"] == uid
        assert dashboard["title"] == title
        assert 6 <= len(dashboard["panels"]) <= 12
        assert all(panel["datasource"]["uid"] == "prometheus" for panel in dashboard["panels"])


@pytest.mark.unit
def test_grafana_network_policy_is_gateway_scoped() -> None:
    policy = load(OBSERVABILITY / "grafana-network-policy.yaml")["spec"]
    sources = policy["ingress"][0]["fromEndpoints"]
    assert sources[0]["matchLabels"] == {
        "k8s:io.kubernetes.pod.namespace": "envoy-gateway-system",
        "k8s:gateway.envoyproxy.io/owning-gateway-name": "platform",
    }
    assert policy["ingress"][0]["toPorts"][0]["ports"] == [{"port": "3000", "protocol": "TCP"}]


@pytest.mark.unit
def test_collector_is_pinned_filelog_and_otlp_daemonset() -> None:
    resources = list(
        yaml.safe_load_all(
            (OBSERVABILITY / "opentelemetry-collector.yaml").read_text(encoding="utf-8")
        )
    )
    config = next(item for item in resources if item["kind"] == "ConfigMap")["data"]["config.yaml"]
    daemonset = next(item for item in resources if item["kind"] == "DaemonSet")
    container = daemonset["spec"]["template"]["spec"]["containers"][0]

    assert container["image"] == "docker.io/otel/opentelemetry-collector-contrib:0.159.0"
    assert "filelog:" in config
    assert "otlp:" in config
    assert "otlphttp/loki:" in config
    assert "http://loki.monitoring.svc.cluster.local:3100/otlp" in config
    assert "otlphttp/tempo:" in config
    assert "http://tempo.monitoring.svc.cluster.local:4318" in config
    assert "memory_limiter" in config
    assert "k8sattributes" in config
    assert (
        "privileged" not in container["securityContext"]
        or container["securityContext"]["privileged"] is False
    )
    assert all(
        not mount.get("mountPath", "").startswith("/var/run") for mount in container["volumeMounts"]
    )


@pytest.mark.unit
def test_telemetry_backends_are_internal_and_policy_scoped() -> None:
    policies = list(
        yaml.safe_load_all(
            (OBSERVABILITY / "telemetry-network-policies.yaml").read_text(encoding="utf-8")
        )
    )
    assert {policy["metadata"]["name"] for policy in policies} == {
        "loki",
        "opentelemetry-collector",
        "tempo",
    }
    collector = next(
        policy for policy in policies if policy["metadata"]["name"] == "opentelemetry-collector"
    )
    allowed_namespace = collector["spec"]["ingress"][0]["from"][0]["namespaceSelector"][
        "matchLabels"
    ]
    assert allowed_namespace == {"telemetry.lap.dev/otlp": "enabled"}


@pytest.mark.unit
def test_part_two_alerts_cover_pipeline_failures() -> None:
    groups = load(OBSERVABILITY / "prometheus-rules.yaml")["spec"]["groups"]
    telemetry = next(group for group in groups if group["name"] == "lap.telemetry")
    assert {rule["alert"] for rule in telemetry["rules"]} == {
        "LokiUnavailable",
        "OpenTelemetryCollectorUnavailable",
        "OpenTelemetryExporterFailures",
        "TelemetryStorageCapacityCritical",
        "TempoUnavailable",
    }
