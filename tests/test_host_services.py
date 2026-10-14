from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
ANSIBLE = ROOT / "infrastructure" / "ansible"


def test_host_services_play_targets_only_load_balancers() -> None:
    playbook = yaml.safe_load((ANSIBLE / "playbooks" / "host-services.yaml").read_text())
    assert len(playbook) == 1
    assert playbook[0]["hosts"] == "load_balancers"
    assert playbook[0]["roles"] == [{"role": "load_balancer"}]


def test_haproxy_uses_vip_and_all_generated_control_planes() -> None:
    template = (ANSIBLE / "roles/load_balancer/templates/haproxy.cfg.j2").read_text()
    assert "bind {{ lab.network.api_virtual_ip }}:{{ lap_api_port }}" in template
    assert "groups['control_planes'] | sort" in template
    assert "tcp-check connect port {{ lap_api_port }}" in template


def test_keepalived_discovers_interface_and_uses_unicast_peer() -> None:
    tasks = (ANSIBLE / "roles/load_balancer/tasks/main.yaml").read_text()
    template = (ANSIBLE / "roles/load_balancer/templates/keepalived.conf.j2").read_text()
    assert "Discover interface carrying the inventory address" in tasks
    assert "interface {{ lap_lab_interface }}" in template
    assert "unicast_src_ip {{ ansible_host }}" in template
    assert "virtual_ipaddress" in template


def test_dns_records_come_from_authoritative_variables() -> None:
    defaults = (ANSIBLE / "roles/load_balancer/defaults/main.yaml").read_text()
    template = (ANSIBLE / "roles/load_balancer/templates/dnsmasq.conf.j2").read_text()
    assert "lab.hostnames.platform_api" in defaults
    assert "lab.hostnames.registry" in defaults
    assert "lab.hostnames.build_agent" in defaults
    assert "lab.hostnames.grafana" in defaults
    wildcard = "address=/{{ lab.lab.application_domain }}/{{ lab.network.envoy_gateway_ip }}"
    assert wildcard in template


def test_firewall_is_default_deny_and_does_not_open_public_wildcards() -> None:
    template = (ANSIBLE / "roles/load_balancer/templates/nftables.conf.j2").read_text()
    assert "table inet lap" in template
    assert template.count("policy drop") == 2
    assert "lap_admin_address" in template
    assert "0.0.0.0/0" not in template
    assert "tcp dport { 22, 53, {{ lap_api_port }} }" in template


def test_failover_test_guarantees_service_restoration() -> None:
    playbook = (ANSIBLE / "playbooks/test-keepalived-failover.yaml").read_text()
    assert "Stop Keepalived on the preferred node" in playbook
    assert "always:" in playbook
    assert "Restore Keepalived on the preferred node" in playbook
    assert "Assert restored deterministic steady state" in playbook
