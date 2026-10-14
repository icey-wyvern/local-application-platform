from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
NETWORKING = ROOT / "cluster" / "validation" / "networking"


def test_host_firewall_opens_only_required_cilium_paths() -> None:
    template = (
        ROOT / "infrastructure/ansible/roles/kubernetes_node/templates/nftables.conf.j2"
    ).read_text()
    assert "udp dport 8472 accept" in template
    assert "tcp dport 4240 accept" in template
    assert template.count("lab.network.pod_cidr") >= 3
    assert "tcp dport { 9100, 10250 } accept" in template
    assert "Cilium masquerades pod-to-host traffic" in template
    assert template.count("policy drop") == 2
    assert "flush ruleset" not in template


def test_networking_validation_is_worker_scoped_and_temporary() -> None:
    expected_nodes = {"worker-1", "worker-2", "worker-3"}
    observed_nodes: set[str] = set()
    images: set[str] = set()
    for path in NETWORKING.glob("*.yaml"):
        document = yaml.safe_load(path.read_text())
        if document.get("kind") != "Pod":
            continue
        observed_nodes.add(document["spec"]["nodeName"])
        images.update(container["image"] for container in document["spec"]["containers"])
        assert document["spec"]["automountServiceAccountToken"] is False
    assert observed_nodes == expected_nodes
    assert images == {"busybox:1.37.0"}

    validation = (ROOT / "tools/validate-cilium-networking.sh").read_text()
    assert "trap cleanup" in validation
    assert "delete --kustomize" in validation
    assert "http://example.com/" in validation
    assert "cilium-dbg status --brief" in validation


def test_networking_slice_does_not_install_later_components() -> None:
    files = [
        ROOT / "tools/install-cilium.sh",
        ROOT / "tools/validate-cilium-networking.sh",
        ROOT / "infrastructure/ansible/playbooks/kubernetes-networking-host.yaml",
    ]
    content = "\n".join(path.read_text() for path in files)
    for component in (
        "metallb",
        "envoy-gateway",
        "cert-manager",
        "longhorn",
        "postgresql",
    ):
        assert component not in content.lower()


def test_failure_test_replaces_only_one_cilium_agent() -> None:
    validation = (ROOT / "tools/validate-cilium-networking.sh").read_text()
    assert "--field-selector spec.nodeName=worker-1" in validation
    assert 'delete pod "$old_pod"' in validation
    assert 'test "$new_uid" != "$old_uid"' in validation
    assert "delete daemonset" not in validation
