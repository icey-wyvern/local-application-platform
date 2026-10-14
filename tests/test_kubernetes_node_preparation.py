from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
ANSIBLE = ROOT / "infrastructure" / "ansible"
ROLE = ANSIBLE / "roles" / "kubernetes_node"


def test_playbook_targets_only_kubernetes_nodes() -> None:
    playbook = yaml.safe_load((ANSIBLE / "playbooks/kubernetes-node-prepare.yaml").read_text())
    assert len(playbook) == 1
    assert playbook[0]["hosts"] == "control_planes:workers"
    assert playbook[0]["roles"] == [{"role": "kubernetes_node"}]


def test_role_uses_all_authoritative_runtime_versions() -> None:
    defaults = (ROLE / "defaults/main.yaml").read_text()
    tasks = (ROLE / "tasks/main.yaml").read_text()
    for name in ("kubernetes", "containerd", "runc", "cni_plugins"):
        assert f"versions.{name}" in defaults + tasks
    assert "latest" not in defaults


def test_containerd_uses_native_v4_cri_and_systemd_cgroups() -> None:
    template = (ROLE / "templates/containerd.toml.j2").read_text()
    assert "version = 4" in template
    assert "io.containerd.cri.v1.runtime" in template
    assert 'BinaryName = "/usr/local/sbin/runc"' in template
    assert "SystemdCgroup = true" in template
    assert 'bin_dirs = ["/opt/cni/bin"]' in template


def test_node_os_preparation_is_persistent() -> None:
    tasks = (ROLE / "tasks/main.yaml").read_text()
    modules = (ROLE / "templates/modules.conf.j2").read_text()
    sysctls = (ROLE / "templates/sysctl.conf.j2").read_text()
    assert "Disable persistent fstab swap entries" in tasks
    assert {"overlay", "br_netfilter"} <= set(modules.splitlines())
    assert "net.ipv4.ip_forward = 1" in sysctls
    assert "net.bridge.bridge-nf-call-iptables = 1" in sysctls


def test_packages_are_exact_and_held() -> None:
    tasks = (ROLE / "tasks/main.yaml").read_text()
    assert "kubelet={{ lap_kubernetes_deb_version }}" in tasks
    assert "kubeadm={{ lap_kubernetes_deb_version }}" in tasks
    assert "kubectl={{ lap_kubernetes_deb_version }}" in tasks
    assert "selection: hold" in tasks


def test_role_does_not_bootstrap_or_join() -> None:
    role_text = "\n".join(path.read_text() for path in ROLE.rglob("*.*"))
    assert "kubeadm init" not in role_text
    assert "kubeadm join" not in role_text
    assert "kube-apiserver" not in role_text


def test_firewall_is_role_scoped_and_preserves_external_tables() -> None:
    template = (ROLE / "templates/nftables.conf.j2").read_text()
    assert "destroy table inet lap" in template
    assert "flush ruleset" not in template
    assert "lap_role == 'control_plane'" in template
    assert template.count("policy drop") == 2
