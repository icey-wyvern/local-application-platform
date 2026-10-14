from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
METALLB = ROOT / "cluster" / "metallb"
VALIDATION = ROOT / "cluster" / "validation" / "metallb"


def _documents(path: Path) -> list[dict[str, Any]]:
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def test_address_pools_partition_authoritative_range_and_reserve_envoy_ip() -> None:
    config = yaml.safe_load((ROOT / "config/lab.yaml").read_text())
    network = config["network"]
    pools = {
        document["metadata"]["name"]: document["spec"]
        for document in _documents(METALLB / "address-pools.yaml")
    }

    assert pools == {
        "envoy-gateway-reserved": {
            "addresses": [f"{network['envoy_gateway_ip']}/32"],
            "autoAssign": False,
        },
        "lab-general": {
            "addresses": [
                f"{IPv4Address(network['metallb_pool_start']) + 1}-{network['metallb_pool_end']}"
            ],
            "autoAssign": True,
        },
    }


def test_configuration_is_layer2_only() -> None:
    advertisement = yaml.safe_load((METALLB / "l2-advertisement.yaml").read_text())
    assert advertisement["kind"] == "L2Advertisement"
    assert advertisement["spec"]["ipAddressPools"] == [
        "envoy-gateway-reserved",
        "lab-general",
    ]
    assert not list(METALLB.glob("*bgp*"))


def test_validation_service_uses_non_reserved_ip_and_is_temporary() -> None:
    config = yaml.safe_load((ROOT / "config/lab.yaml").read_text())
    network = config["network"]
    service = yaml.safe_load((VALIDATION / "service.yaml").read_text())
    requested = service["metadata"]["annotations"]["metallb.io/loadBalancerIPs"]
    assert requested == str(IPv4Address(network["metallb_pool_start"]) + 1)
    assert requested != str(network["envoy_gateway_ip"])
    assert service["spec"]["type"] == "LoadBalancer"

    script = (ROOT / "tools/validate-metallb.sh").read_text()
    assert "trap cleanup" in script
    assert "remove_validation_resources" in script
    assert "Invoke-WebRequest" in script
    assert "Get-NetNeighbor" in script
    assert "exclude-from-external-load-balancers" in script
    assert "restore_leader" in script


def test_metallb_firewall_is_memberlist_only_and_node_scoped() -> None:
    template = (
        ROOT / "infrastructure/ansible/roles/kubernetes_node/templates/nftables.conf.j2"
    ).read_text()
    assert "meta l4proto { tcp, udp } th dport 7946 accept" in template
    assert "groups['control_planes'] + groups['workers']" in template
    assert template.count("policy drop") == 2
    assert "flush ruleset" not in template


def test_metallb_slice_does_not_install_later_components() -> None:
    content = "\n".join(
        path.read_text()
        for path in (
            ROOT / "tools/install-metallb.sh",
            ROOT / "tools/validate-metallb.sh",
            METALLB / "kustomization.yaml",
        )
    ).lower()
    for component in ("cert-manager", "longhorn", "postgresql", "observability"):
        assert component not in content
