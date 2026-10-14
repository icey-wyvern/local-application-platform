from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
POSTGRESQL = REPO_ROOT / "cluster" / "postgresql"
VALIDATION = REPO_ROOT / "cluster" / "validation" / "postgresql"
EXPECTED_IMAGE = (
    "postgres:18.6-bookworm@sha256:3725f4e2499eef5134592b3b4ab79a543ed7f8e533b05b5b637af926630f6650"
)


def load(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.mark.unit
def test_postgresql_statefulset_is_pinned_and_persistent() -> None:
    statefulset = load(POSTGRESQL / "statefulset.yaml")
    spec = statefulset["spec"]
    assert isinstance(spec, dict)
    template = spec["template"]
    assert isinstance(template, dict)
    pod_spec = template["spec"]
    assert isinstance(pod_spec, dict)
    containers = pod_spec["containers"]
    init_containers = pod_spec["initContainers"]
    assert isinstance(containers, list)
    assert isinstance(init_containers, list)
    assert containers[0]["image"] == EXPECTED_IMAGE
    assert init_containers[0]["image"] == EXPECTED_IMAGE
    assert pod_spec["nodeSelector"] == {"node.longhorn.io/create-default-disk": "true"}
    claim = spec["volumeClaimTemplates"][0]["spec"]
    assert claim["storageClassName"] == "longhorn"
    assert claim["resources"]["requests"]["storage"] == "10Gi"
    assert containers[0]["env"][0] == {
        "name": "PGDATA",
        "value": "/var/lib/postgresql/18/docker",
    }
    assert {probe for probe in ("startupProbe", "readinessProbe", "livenessProbe")} <= set(
        containers[0]
    )


@pytest.mark.unit
def test_postgresql_tls_authentication_and_tuning_are_explicit() -> None:
    configuration = load(POSTGRESQL / "configuration.yaml")["data"]
    assert isinstance(configuration, dict)
    postgres = configuration["postgresql.conf"]
    hba = configuration["pg_hba.conf"]
    for expected in (
        "max_connections = 100",
        "shared_buffers = '256MB'",
        "work_mem = '4MB'",
        "maintenance_work_mem = '64MB'",
        "effective_cache_size = '768MB'",
        "ssl = on",
        "password_encryption = 'scram-sha-256'",
    ):
        assert expected in postgres
    assert "hostnossl all all 0.0.0.0/0       reject" in hba
    assert "hostssl all all 0.0.0.0/0         scram-sha-256" in hba
    certificate = load(POSTGRESQL / "certificate.yaml")["spec"]
    assert isinstance(certificate, dict)
    assert certificate["issuerRef"]["name"] == "lap-cluster-intermediate"
    assert "postgresql.platform-database.svc.cluster.local" in certificate["dnsNames"]


@pytest.mark.unit
def test_postgresql_is_internal_and_identity_restricted() -> None:
    service = load(POSTGRESQL / "service.yaml")
    assert service["spec"]["type"] == "ClusterIP"
    policy = load(POSTGRESQL / "network-policy.yaml")["spec"]
    source = policy["ingress"][0]["fromEndpoints"][0]["matchLabels"]
    assert source == {
        "k8s:io.kubernetes.pod.namespace": "platform-system",
        "io.cilium.k8s.policy.serviceaccount": "platform-api",
        "k8s:app.kubernetes.io/name": "platform-api",
    }
    assert policy["ingress"][0]["toPorts"][0]["ports"] == [{"port": "5432", "protocol": "TCP"}]


@pytest.mark.unit
def test_validation_resources_are_disposable_and_version_pinned() -> None:
    clients = list(yaml.safe_load_all((VALIDATION / "clients.yaml").read_text(encoding="utf-8")))
    assert {item["metadata"]["namespace"] for item in clients} == {
        "postgresql-validation",
        "postgresql-denied",
    }
    assert all(item["spec"]["containers"][0]["image"] == EXPECTED_IMAGE for item in clients)
