from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
ANSIBLE = ROOT / "infrastructure" / "ansible"
ROLE = ANSIBLE / "roles" / "kubeadm_cluster"


def test_bootstrap_targets_only_expected_cluster_nodes() -> None:
    playbook = yaml.safe_load((ANSIBLE / "playbooks/kubernetes-bootstrap.yaml").read_text())
    remote_patterns = {play["hosts"] for play in playbook if play["hosts"] != "localhost"}
    assert remote_patterns <= {
        "control_planes:workers",
        "control_planes",
        "control_planes:!cp-1",
        "workers",
        "cp-1",
    }
    assert "load_balancers" not in remote_patterns
    assert "build_machines" not in remote_patterns


def test_kubeadm_configuration_uses_authoritative_ha_networking() -> None:
    template = (ROLE / "templates/init-config.yaml.j2").read_text()
    assert "kubeadm.k8s.io/v1beta4" in template
    assert "lap_kubeadm_control_plane_endpoint" in template
    assert "lab.network.pod_cidr" in template
    assert "lab.network.service_cidr" in template
    assert "proxy:\n  disabled: false" in template
    assert "etcd:\n  local:" in template
    assert "cgroupDriver: systemd" in template


def test_control_plane_join_preserves_taints_and_verifies_ca() -> None:
    template = (ROLE / "templates/join-control-plane.yaml.j2").read_text()
    assert "unsafeSkipCAVerification: false" in template
    assert "caCertHashes:" in template
    assert "certificateKey:" in template
    assert "taints:" not in template


def test_join_material_is_short_lived_and_not_persisted() -> None:
    defaults = (ROLE / "defaults/main.yaml").read_text()
    join_tasks = (ROLE / "tasks/join-material.yaml").read_text()
    cleanup = (ROLE / "tasks/cleanup-token.yaml").read_text()
    worker_join = (ROLE / "tasks/join-worker.yaml").read_text()
    control_plane_join = (ROLE / "tasks/join-control-plane.yaml").read_text()
    assert "lap_kubeadm_token_ttl: 15m" in defaults
    assert "no_log: true" in join_tasks
    assert "List residual kubeadm bootstrap tokens" in cleanup
    assert "Delete residual kubeadm bootstrap tokens" in cleanup
    assert "kubeadm-certs" in cleanup
    assert "state: absent" in worker_join
    assert "state: absent" in control_plane_join


def test_secret_encryption_is_configured_without_a_repository_key() -> None:
    encryption = (ROLE / "templates/encryption-config.yaml.j2").read_text()
    init = (ROLE / "templates/init-config.yaml.j2").read_text()
    assert "secretbox:" in encryption
    assert "lap_encryption_key.stdout" in encryption
    assert "encryption-provider-config" in init
    assert "secret:" not in init
    assert not any(path.suffix in {".key", ".conf"} for path in ROLE.rglob("*"))


def test_wsl_kubeconfig_stays_outside_repository() -> None:
    defaults = (ROLE / "defaults/main.yaml").read_text()
    playbook = (ANSIBLE / "playbooks/kubernetes-bootstrap.yaml").read_text()
    assert ".kube/lap-admin.conf" in defaults
    assert ".kube/lap-admin.conf" in playbook
    assert ".generated" not in defaults
    assert 'mode: "0600"' in playbook


def test_ha_test_restores_static_pod_manifest_in_always_block() -> None:
    playbook = yaml.safe_load(
        (ANSIBLE / "playbooks/test-kubernetes-control-plane-ha.yaml").read_text()
    )
    tasks = playbook[0]["tasks"]
    exercise = next(task for task in tasks if "block" in task)
    assert "always" in exercise
    rendered = (ANSIBLE / "playbooks/test-kubernetes-control-plane-ha.yaml").read_text()
    assert "cp-2" in rendered
    assert "--raw=/readyz" in rendered
    assert "lap_restored_backend_state.stdout == 'UP'" in rendered
