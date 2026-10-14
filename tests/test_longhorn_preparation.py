from pathlib import Path
from typing import Any, cast

import yaml
from lap_config.generate import load_config, render_files

ROOT = Path(__file__).parents[1]


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text()))


def test_longhorn_pin_and_v1_only_storage_contract() -> None:
    lab, versions = load_config(ROOT)
    rendered = {item.path.name: item.content for item in render_files(lab, versions, ROOT)}
    values = yaml.safe_load(rendered["longhorn.values.yaml"])
    settings = values["defaultSettings"]
    storage_class = values["persistence"]

    assert versions.longhorn == "1.12.1"
    assert settings["v1DataEngine"] is True
    assert settings["v2DataEngine"] is False
    assert settings["defaultDataPath"] == "/var/lib/longhorn"
    assert settings["storageReservedPercentageForDefaultDisk"] == 30
    assert settings["storageOverProvisioningPercentage"] == 100
    assert storage_class["defaultClassReplicaCount"] == 3
    assert storage_class["volumeBindingMode"] == "WaitForFirstConsumer"
    assert "LONGHORN_VERSION := 1.12.1" in rendered["generated.mk"]
    assert "LONGHORN_WORKERS := worker-1 worker-2 worker-3" in rendered["generated.mk"]


def test_worker_prerequisites_and_firewall_are_narrowly_scoped() -> None:
    role = (ROOT / "infrastructure/ansible/roles/longhorn_host/tasks/main.yaml").read_text()
    defaults = (ROOT / "infrastructure/ansible/roles/longhorn_host/defaults/main.yaml").read_text()
    firewall = (
        ROOT / "infrastructure/ansible/roles/kubernetes_node/templates/nftables.conf.j2"
    ).read_text()

    assert (
        "hosts: workers"
        in (ROOT / "infrastructure/ansible/playbooks/longhorn-host-prepare.yaml").read_text()
    )
    for package in ("open-iscsi", "nfs-common", "cryptsetup", "dmsetup"):
        assert package in defaults
    assert "iscsi_tcp" in defaults
    assert "dm_crypt" in defaults
    assert "multipathd.service" in role
    assert "state: directory" in role
    assert "filesystem" not in role
    assert "partition" not in role
    assert "inventory_hostname in groups['workers']" in firewall
    for port in ("2049", "3260", "8500-8504", "10000-30000"):
        assert port in firewall
    assert "lab.network.pod_cidr" in firewall


def test_installation_is_idempotent_and_validation_cleans_up() -> None:
    installer = (ROOT / "tools/install-longhorn.sh").read_text()
    validator = (ROOT / "tools/validate-longhorn-storage.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()

    assert "longhorn-install: longhorn-host-prepare longhorn-render" in makefile
    assert "longhorn-failure-test:" in makefile
    assert "already matches the generated Helm configuration" in installer
    assert 'helm_bin" --kubeconfig' in installer
    assert "trap cleanup" in validator
    assert "kubectl_cmd drain" in validator
    assert "replicas.longhorn.io" in validator
    assert "delete --kustomize" in validator


def test_longhorn_namespace_declares_required_pod_security_level() -> None:
    namespace = _load(ROOT / "cluster/longhorn/namespace.yaml")
    labels = namespace["metadata"]["labels"]

    assert namespace["metadata"]["name"] == "longhorn-system"
    assert labels["pod-security.kubernetes.io/enforce"] == "privileged"


def test_validation_volume_is_temporary_restricted_and_explicit() -> None:
    validation = ROOT / "cluster/validation/longhorn"
    namespace = _load(validation / "namespace.yaml")
    pvc = _load(validation / "pvc.yaml")
    deployment = _load(validation / "deployment.yaml")

    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == ("restricted")
    assert pvc["spec"]["storageClassName"] == "longhorn"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
