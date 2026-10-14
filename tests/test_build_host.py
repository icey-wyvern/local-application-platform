from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[1]
ROLE = ROOT / "infrastructure/ansible/roles/build_host"


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text()))


def test_build_host_versions_and_layout_are_authoritative() -> None:
    versions = _load(ROOT / "config/versions.yaml")
    lab = _load(ROOT / "config/lab.yaml")

    assert versions["registry"] == "3.0.0"
    assert versions["buildkit"] == "0.30.0"
    assert versions["skopeo"] == "1.23.0"
    assert versions["trivy"] == "0.73.0"
    assert lab["registry"] == {
        "implementation": "distribution",
        "host": "registry.lab.test",
        "port": 5000,
        "image_namespace": "apps",
        "data_path": "/var/lib/lap-registry",
        "service_user": "lap-registry",
        "publisher_identity": "lap-registry-publisher",
        "pull_identity": "lap-registry-puller",
    }
    assert lab["build_host"]["buildkit_socket"].startswith("/run/")
    assert lab["build_host"]["artifact_path"].startswith(lab["build_agent"]["state_path"])


def test_registry_uses_tls_auth_persistence_and_a_dedicated_identity() -> None:
    config = (ROLE / "templates/registry-config.yml.j2").read_text()
    unit = (ROLE / "templates/lap-registry.service.j2").read_text()
    tasks = (ROLE / "tasks/main.yaml").read_text()

    assert "rootdirectory: {{ lab.registry.data_path }}" in config
    assert "certificate:" in config and "key:" in config
    assert "htpasswd:" in config
    assert "User={{ lab.registry.service_user }}" in unit
    assert "ReadWritePaths={{ lab.registry.data_path }}" in unit
    assert "--insecure" not in tasks
    assert "tls-verify=false" not in tasks
    assert "no_log: true" in tasks


def test_buildkit_is_rootless_private_and_cgroup_filtered() -> None:
    unit = (ROLE / "templates/lap-buildkit.service.j2").read_text()
    config = (ROLE / "templates/buildkitd.toml.j2").read_text()

    assert "User={{ lab.build_host.buildkit_user }}" in unit
    assert "/usr/bin/rootlesskit" in unit
    assert "Delegate=true" in unit
    assert "CPUQuota={{ lap_buildkit_cpu_quota }}" in unit
    assert "MemoryMax={{ lap_buildkit_memory_max }}" in unit
    entrypoint = (ROLE / "templates/buildkit-entrypoint.sh.j2").read_text()
    assert "lab.network.bootstrap_dns_servers" in entrypoint
    assert ">/etc/resolv.conf" in entrypoint
    for network in (
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
        "fe80::/10",
    ):
        assert f"IPAddressDeny={network}" in unit
    assert "unix://{{ lab.build_host.buildkit_socket }}" in config
    assert "rootless = true" in config
    assert "noProcessSandbox = true" in config
    assert 'snapshotter = "native"' in config
    assert "insecure" not in config
    sysctl = (ROLE / "templates/sysctl.conf.j2").read_text()
    assert "kernel.apparmor_restrict_unprivileged_userns = 0" in sysctl


def test_exact_artifact_validation_scans_before_skopeo_publication() -> None:
    validation = (ROOT / "infrastructure/ansible/playbooks/validate-build-host.yaml").read_text()
    scan = validation.index("Scan exact OCI content")
    publish = validation.index("Publish the already-scanned OCI archive")

    assert scan < publish
    assert "type=oci,dest={{ lap_smoke_archive }}" in validation
    assert "trivy\n          - image\n          - --input" in validation
    assert "skopeo\n          - copy" in validation
    assert "--preserve-digests" in validation
    assert (
        "lap_scanned_manifest_digest.stdout == lap_published_manifest_digest.stdout" in validation
    )
    assert "--output\n          - type=image" not in validation
    assert "--insecure" not in validation
    assert "tls-verify=false" not in validation


def test_host_issued_registry_key_stays_out_of_git_and_root_key_is_not_copied() -> None:
    pki = (ROOT / "tools/prepare-build-host-pki.ps1").read_text()
    tasks = (ROLE / "tasks/main.yaml").read_text()
    ignore = (ROOT / ".gitignore").read_text()

    assert "'-days', '90'" in pki
    assert "extendedKeyUsage = serverAuth" in pki
    assert "refusing to overwrite" in pki
    assert ".generated/" in ignore
    assert "lab-root-ca.key" not in tasks
    assert "registry-lab-test.key" in tasks


def test_build_host_firewall_exposes_only_ssh_and_registry() -> None:
    firewall = (ROLE / "templates/nftables.conf.j2").read_text()

    assert "policy drop" in firewall
    assert "tcp dport 22 accept" in firewall
    assert "tcp dport {{ lab.registry.port }} accept" in firewall
    assert "lap_build_admin_address" in firewall
    assert "8443" not in firewall
    assert "NodePort" not in firewall
