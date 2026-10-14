from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from lap_config.generate import load_config, load_yaml, render_files
from lap_config.models import LabConfig
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.unit
def test_public_lab_example_is_valid() -> None:
    example = LabConfig.model_validate(load_yaml(REPO_ROOT / "config/lab.example.yaml"))

    assert example.hyperv.ssh_public_key_path == "~/.ssh/lap_lab_ed25519.pub"


@pytest.mark.unit
def test_repository_config_is_valid() -> None:
    lab, versions = load_config(REPO_ROOT)

    assert len(lab.virtual_machines) == 9
    assert versions.kubernetes == "1.36.2"
    assert versions.terraform_provider_hyperv == "0.3.1"
    assert lab.lab.host_virtualisation == "Hyper-V"
    assert lab.network.api_virtual_ip.exploded == "10.20.0.12"
    assert (
        str(lab.hyperv.base_image_source)
        == "https://cloud-images.ubuntu.com/releases/noble/release-20260826/ubuntu-24.04-server-cloudimg-amd64-azure.vhd.tar.gz"
    )


@pytest.mark.unit
def test_generated_outputs_include_required_targets() -> None:
    lab, versions = load_config(REPO_ROOT)

    generated_paths = {
        item.path.relative_to(REPO_ROOT).as_posix()
        for item in render_files(lab, versions, REPO_ROOT)
    }

    assert ".generated/terraform/lab.auto.tfvars.json" in generated_paths
    assert ".generated/ansible/inventory.yaml" in generated_paths
    assert ".generated/ansible/group_vars/all/generated.yaml" in generated_paths
    assert ".generated/platform/platform.env" in generated_paths
    assert ".generated/make/generated.mk" in generated_paths

    terraform_file = next(
        item
        for item in render_files(lab, versions, REPO_ROOT)
        if item.path.name == "lab.auto.tfvars.json"
    )
    assert json.loads(terraform_file.content)["ssh_public_key_path"] == (
        lab.hyperv.ssh_public_key_path
    )


@pytest.mark.unit
def test_generated_cilium_values_match_networking_contract() -> None:
    lab, versions = load_config(REPO_ROOT)
    generated = next(
        item
        for item in render_files(lab, versions, REPO_ROOT)
        if item.path.name == "cilium.values.yaml"
    )
    values = yaml.safe_load(generated.content)
    generated_make = next(
        item for item in render_files(lab, versions, REPO_ROOT) if item.path.name == "generated.mk"
    ).content

    assert versions.cilium == "1.20.1"
    assert str(lab.network.pod_cidr) == "10.244.0.0/16"
    assert lab.network.pod_node_cidr_mask == 24
    assert values["ipam"]["mode"] == "kubernetes"
    assert values["k8s"]["requireIPv4PodCIDR"] is True
    assert values["kubeProxyReplacement"] is False
    assert values["routingMode"] == "tunnel"
    assert values["tunnelProtocol"] == "vxlan"
    assert values["hubble"]["enabled"] is False
    assert values["envoy"]["enabled"] is False
    assert "version" not in values
    assert "podCIDR" not in values
    assert "API_VIRTUAL_IP := 10.20.0.12" in generated_make
    assert "POD_CIDR := 10.244.0.0/16" in generated_make
    assert "POD_NODE_CIDR_MASK := 24" in generated_make


@pytest.mark.unit
def test_generated_metallb_values_match_layer2_contract() -> None:
    lab, versions = load_config(REPO_ROOT)
    generated = next(
        item
        for item in render_files(lab, versions, REPO_ROOT)
        if item.path.name == "metallb.values.yaml"
    )
    values = yaml.safe_load(generated.content)
    generated_make = next(
        item for item in render_files(lab, versions, REPO_ROOT) if item.path.name == "generated.mk"
    ).content

    assert versions.metallb == "0.16.1"
    assert values["controller"]["enabled"] is True
    assert values["speaker"]["enabled"] is True
    assert values["speaker"]["tolerateMaster"] is True
    assert values["speaker"]["memberlist"] == {
        "enabled": True,
        "mlBindPort": 7946,
    }
    assert values["speaker"]["frr"]["enabled"] is False
    assert values["frrk8s"]["enabled"] is False
    assert values["prometheus"]["podMonitor"]["enabled"] is False
    assert values["prometheus"]["serviceMonitor"]["enabled"] is False
    assert "METALLB_VERSION := 0.16.1" in generated_make
    assert "METALLB_POOL_START := 10.20.0.100" in generated_make
    assert "METALLB_POOL_END := 10.20.0.120" in generated_make
    assert "METALLB_TEST_IP := 10.20.0.101" in generated_make
    assert "ENVOY_GATEWAY_IP := 10.20.0.100" in generated_make


@pytest.mark.unit
def test_coredns_routes_lab_domain_only_to_authoritative_resolver() -> None:
    lab, versions = load_config(REPO_ROOT)
    rendered = {item.path.name: item.content for item in render_files(lab, versions, REPO_ROOT)}
    config = yaml.safe_load(rendered["coredns.yaml"])
    corefile = config["data"]["Corefile"]

    assert config["metadata"] == {"name": "coredns", "namespace": "kube-system"}
    assert "lab.test:53 {\n    errors\n    forward . 10.20.0.12" in corefile
    assert "forward . /etc/resolv.conf" in corefile
    assert "kubernetes cluster.local in-addr.arpa ip6.arpa" in corefile


@pytest.mark.unit
def test_generated_envoy_gateway_values_separate_crd_ownership() -> None:
    lab, versions = load_config(REPO_ROOT)
    rendered = {item.path.name: item.content for item in render_files(lab, versions, REPO_ROOT)}
    values = yaml.safe_load(rendered["envoy-gateway.values.yaml"])
    crd_values = yaml.safe_load(rendered["envoy-gateway-crds.values.yaml"])

    assert versions.envoy_gateway == "1.9.1"
    assert versions.gateway_api == "1.5.1"
    assert values["crds"]["enabled"] is False
    assert values["hpa"]["enabled"] is False
    assert values["service"]["type"] == "ClusterIP"
    assert values["config"]["envoyGateway"]["extensionApis"] == {}
    assert values["config"]["envoyGateway"]["gateway"]["controllerName"] == (
        "gateway.envoyproxy.io/gatewayclass-controller"
    )
    assert crd_values["crds"]["gatewayAPI"]["enabled"] is False
    assert crd_values["crds"]["gatewayAPI"]["channel"] == "standard"
    assert crd_values["crds"]["envoyGateway"]["enabled"] is True
    assert "ENVOY_GATEWAY_VERSION := 1.9.1" in rendered["generated.mk"]
    assert "GATEWAY_API_VERSION := 1.5.1" in rendered["generated.mk"]


@pytest.mark.unit
def test_generated_cert_manager_values_are_minimal_and_pinned() -> None:
    lab, versions = load_config(REPO_ROOT)
    rendered = {item.path.name: item.content for item in render_files(lab, versions, REPO_ROOT)}
    values = yaml.safe_load(rendered["cert-manager.values.yaml"])

    assert versions.cert_manager == "1.21.1"
    assert values == {
        "cainjector": {"enabled": True, "replicaCount": 1},
        "crds": {"enabled": True, "keep": True},
        "prometheus": {"enabled": False},
        "replicaCount": 1,
        "startupapicheck": {"enabled": True},
        "webhook": {"replicaCount": 1},
    }
    assert "CERT_MANAGER_VERSION := 1.21.1" in rendered["generated.mk"]


@pytest.mark.unit
def test_postgresql_contract_is_authoritative_and_pinned() -> None:
    lab, versions = load_config(REPO_ROOT)
    rendered = {item.path.name: item.content for item in render_files(lab, versions, REPO_ROOT)}

    assert versions.postgresql == "18.6"
    assert lab.postgresql.namespace == "platform-database"
    assert lab.postgresql.service_name == "postgresql"
    assert lab.postgresql.database == "platform"
    assert lab.postgresql.application_user == "platform_api"
    assert lab.postgresql.storage_class == "longhorn"
    assert lab.postgresql.storage_size == "10Gi"
    assert "POSTGRESQL_VERSION := 18.6" in rendered["generated.mk"]
    assert "POSTGRESQL_STORAGE_CLASS := longhorn" in rendered["generated.mk"]
    assert "POSTGRESQL_STORAGE_SIZE := 10Gi" in rendered["generated.mk"]
    assert (
        "POSTGRESQL_HOST=postgresql.platform-database.svc.cluster.local" in rendered["platform.env"]
    )
    assert (
        "OTEL_EXPORTER_OTLP_ENDPOINT="
        "http://opentelemetry-collector.monitoring.svc.cluster.local:4318"
        in rendered["platform.env"]
    )


@pytest.mark.unit
def test_generated_observability_values_match_part_one_contract() -> None:
    lab, versions = load_config(REPO_ROOT)
    rendered = {item.path.name: item.content for item in render_files(lab, versions, REPO_ROOT)}
    values = yaml.safe_load(rendered["kube-prometheus-stack.values.yaml"])

    assert versions.kube_prometheus_stack_chart == "88.5.4"
    assert values["prometheus"]["prometheusSpec"]["retention"] == "7d"
    assert values["prometheus"]["prometheusSpec"]["retentionSize"] == "8GB"
    assert (
        values["prometheus"]["prometheusSpec"]["storageSpec"]["volumeClaimTemplate"]["spec"][
            "resources"
        ]["requests"]["storage"]
        == "10Gi"
    )
    assert values["grafana"]["persistence"] == {
        "accessModes": ["ReadWriteOnce"],
        "enabled": True,
        "size": "2Gi",
        "storageClassName": "longhorn",
        "type": "sts",
    }
    assert (
        values["alertmanager"]["alertmanagerSpec"]["storage"]["volumeClaimTemplate"]["spec"][
            "resources"
        ]["requests"]["storage"]
        == "2Gi"
    )
    assert values["grafana"]["admin"]["existingSecret"] == "grafana-administrator"
    assert values["grafana"]["ingress"]["enabled"] is False
    assert values["defaultRules"]["create"] is False
    assert values["thanosRuler"]["enabled"] is False
    assert "KUBE_PROMETHEUS_STACK_VERSION := 88.5.4" in rendered["generated.mk"]
    assert "GRAFANA_HOST := grafana.lab.test" in rendered["generated.mk"]


@pytest.mark.unit
def test_generated_observability_part_two_values_are_pinned_and_bounded() -> None:
    lab, versions = load_config(REPO_ROOT)
    rendered = {item.path.name: item.content for item in render_files(lab, versions, REPO_ROOT)}
    loki = yaml.safe_load(rendered["loki.values.yaml"])
    tempo = yaml.safe_load(rendered["tempo.values.yaml"])

    assert versions.loki == "3.7.6"
    assert versions.loki_chart == "18.7.6"
    assert loki["deploymentMode"] == "Monolithic"
    assert loki["singleBinary"]["replicas"] == 1
    assert loki["singleBinary"]["persistence"]["storageClass"] == "longhorn"
    assert loki["singleBinary"]["persistence"]["size"] == "5Gi"
    assert loki["loki"]["limits_config"]["retention_period"] == "72h"
    assert loki["loki"]["compactor"]["delete_request_store"] == "filesystem"
    labels = loki["loki"]["distributor"]["otlp_config"][
        "default_resource_attributes_as_index_labels"
    ]
    assert "k8s.pod.name" not in labels
    assert "service.instance.id" not in labels
    assert loki["gateway"]["enabled"] is False

    assert versions.tempo == "2.10.7"
    assert versions.tempo_chart == "2.2.3"
    assert tempo["tempo"]["tag"] == "2.10.7"
    assert tempo["tempo"]["retention"] == "72h"
    assert tempo["persistence"]["storageClassName"] == "longhorn"
    assert tempo["persistence"]["size"] == "5Gi"
    assert set(tempo["tempo"]["receivers"]) == {"otlp"}
    assert "jaeger:" not in tempo["config"]
    assert "block_retention: 72h" in tempo["config"]
    assert versions.opentelemetry_collector_contrib == "0.159.0"
    assert "LOKI_CHART_VERSION := 18.7.6" in rendered["generated.mk"]
    assert "TEMPO_CHART_VERSION := 2.2.3" in rendered["generated.mk"]
    assert "OTEL_COLLECTOR_VERSION := 0.159.0" in rendered["generated.mk"]


@pytest.mark.unit
def test_lab_requires_exact_nine_machine_topology() -> None:
    lab, _versions = load_config(REPO_ROOT)
    data = lab.model_dump(mode="json")
    data["virtual_machines"] = data["virtual_machines"][:-1]

    with pytest.raises(ValidationError, match="exactly nine"):
        type(lab).model_validate(data)


@pytest.mark.unit
@pytest.mark.parametrize("capacity", [8, 30, 31, 32, 59, 61])
def test_disk_capacity_must_match_amended_parent(capacity: int) -> None:
    lab, _ = load_config(REPO_ROOT)
    data = lab.model_dump(mode="json")
    data["virtual_machines"][0]["disk_gib"] = capacity
    with pytest.raises(ValidationError):
        type(lab).model_validate(data)


@pytest.mark.unit
def test_generated_image_and_network_contract() -> None:
    lab, versions = load_config(REPO_ROOT)
    generated = next(
        item
        for item in render_files(lab, versions, REPO_ROOT)
        if item.path.name == "lab.auto.tfvars.json"
    )
    values = json.loads(generated.content)
    assert values["base_image_capacity_gib"] == 60
    assert all(vm["disk_gib"] == 60 for vm in values["virtual_machines"])
    assert values["base_image_sha256"] == lab.hyperv.base_image_sha256
    assert values["bootstrap_dns_servers"] == ["1.1.1.1", "9.9.9.9"]
    assert values["ssh_user"] == lab.hyperv.ssh_user


def test_generated_build_host_contract_is_pinned() -> None:
    lab, versions = load_config(REPO_ROOT)
    rendered = {item.path.name: item.content for item in render_files(lab, versions, REPO_ROOT)}

    generated_make = rendered["generated.mk"]
    assert "REGISTRY_VERSION := 3.0.0" in generated_make
    assert "REGISTRY_HOST := registry.lab.test" in generated_make
    assert "REGISTRY_ADDRESS := 10.20.0.51" in generated_make
    assert "REGISTRY_PORT := 5000" in generated_make
    assert "REGISTRY_NAMESPACE := apps" in generated_make
    assert "BUILDKIT_VERSION := 0.30.0" in generated_make
    assert "SKOPEO_VERSION := 1.23.0" in generated_make
    assert "TRIVY_VERSION := 0.73.0" in generated_make


@pytest.mark.unit
@pytest.mark.parametrize(
    "field,value", [("base_image_capacity_gib", 30), ("base_image_sha256", "bad")]
)
def test_invalid_parent_contract_is_rejected(field: str, value: object) -> None:
    lab, _ = load_config(REPO_ROOT)
    data = lab.model_dump(mode="json")
    data["hyperv"][field] = value
    with pytest.raises(ValidationError):
        type(lab).model_validate(data)
