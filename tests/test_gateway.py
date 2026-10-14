from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[1]
GATEWAY = ROOT / "cluster" / "gateway"
VALIDATION = ROOT / "cluster" / "validation" / "gateway"


def _load(name: str, *, validation: bool = False) -> dict[str, Any]:
    base = VALIDATION if validation else GATEWAY
    return cast("dict[str, Any]", yaml.safe_load((base / name).read_text()))


def test_shared_gateway_uses_platform_class_and_keeps_http_listener() -> None:
    gateway_class = _load("gateway-class.yaml")
    gateway = _load("gateway.yaml")

    assert gateway_class["metadata"]["name"] == "lap-envoy"
    assert gateway_class["spec"]["controllerName"] == (
        "gateway.envoyproxy.io/gatewayclass-controller"
    )
    assert gateway["spec"]["gatewayClassName"] == "lap-envoy"
    assert gateway["spec"]["infrastructure"]["parametersRef"] == {
        "group": "gateway.envoyproxy.io",
        "kind": "EnvoyProxy",
        "name": "platform",
    }
    listeners = {listener["name"]: listener for listener in gateway["spec"]["listeners"]}
    assert listeners["http"] == {
        "allowedRoutes": {"namespaces": {"from": "All"}},
        "name": "http",
        "port": 80,
        "protocol": "HTTP",
    }


def test_envoy_data_plane_uses_reserved_metallb_address_on_workers() -> None:
    config = yaml.safe_load((ROOT / "config/lab.yaml").read_text())
    envoy_proxy = _load("envoy-proxy.yaml")
    kubernetes = envoy_proxy["spec"]["provider"]["kubernetes"]
    deployment = kubernetes["envoyDeployment"]
    service = kubernetes["envoyService"]

    assert deployment["replicas"] == 2
    expressions = deployment["pod"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"]
    assert expressions == [
        {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}
    ]
    assert service == {
        "annotations": {
            "metallb.io/address-pool": "envoy-gateway-reserved",
            "metallb.io/loadBalancerIPs": config["network"]["envoy_gateway_ip"],
        },
        "externalTrafficPolicy": "Cluster",
        "type": "LoadBalancer",
    }
    assert "hostNetwork" not in deployment["pod"]


def test_temporary_route_targets_two_worker_backends_and_is_cleaned_up() -> None:
    config = yaml.safe_load((ROOT / "config/lab.yaml").read_text())
    deployment = _load("deployment.yaml", validation=True)
    route = _load("route.yaml", validation=True)

    assert deployment["spec"]["replicas"] == 2
    assert route["spec"]["hostnames"] == [f"gateway-smoke.{config['lab']['application_domain']}"]
    assert route["spec"]["parentRefs"] == [
        {"name": "platform", "namespace": "envoy-gateway-system"}
    ]
    assert route["spec"]["rules"][0]["backendRefs"][0] == {
        "name": "gateway-smoke",
        "port": 8080,
    }

    validation_script = (ROOT / "tools/validate-gateway-routing.sh").read_text()
    assert "trap cleanup" in validation_script
    assert 'delete --kustomize "$manifests"' in validation_script
    assert "Invoke-WebRequest" in validation_script
    assert "validate-gateway-routing-hosts.yaml" in validation_script
    assert 'delete pod "$old_pod"' in validation_script


def test_gateway_api_install_is_pinned_to_standard_channel() -> None:
    makefile = (ROOT / "Makefile").read_text()
    installer = (ROOT / "tools/install-gateway-api.sh").read_text()

    assert "GATEWAY_API_STANDARD_SHA256 := " in makefile
    assert "releases/download/v${version}/standard-install.yaml" in installer
    assert "sha256sum --check --status" in installer
    assert "experimental-install" not in installer


def test_large_envoy_crds_use_documented_server_side_apply_path() -> None:
    installer = (ROOT / "tools/install-envoy-gateway.sh").read_text()
    assert '"$helm_binary" template envoy-gateway-crds' in installer
    assert "--field-manager=envoy-gateway-crds" in installer
    assert "upgrade --install envoy-gateway-crds" not in installer


def test_gateway_slice_does_not_install_deferred_components() -> None:
    content = "\n".join(
        path.read_text()
        for path in (
            ROOT / "tools/install-envoy-gateway.sh",
            ROOT / "tools/validate-gateway-routing.sh",
            GATEWAY / "gateway.yaml",
            GATEWAY / "envoy-proxy.yaml",
        )
    ).lower()
    for component in ("cert-manager", "longhorn", "postgresql", "observability"):
        assert component not in content
