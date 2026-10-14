from pathlib import Path

ROOT = Path(__file__).parents[1]
ROLE = ROOT / "infrastructure/ansible/roles/build_host"
AGENT = ROOT / "platform/build-agent/src/lap_build_agent"


def test_agent_service_is_dedicated_hardened_and_mtls_only() -> None:
    unit = (ROLE / "templates/lap-build-agent.service.j2").read_text()
    main = (AGENT / "main.py").read_text()

    assert "User={{ lab.build_agent.service_user }}" in unit
    assert "SupplementaryGroups={{ lap_buildkit_shared_group }}" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "MemoryMax=1G" in unit
    assert "ssl_cert_reqs=2" in main
    assert "ssl_ca_certs=" in main


def test_agent_preserves_exact_artifact_order_and_registry_tls() -> None:
    pipeline = (AGENT / "pipeline.py").read_text()
    scan = pipeline.index('self._stage(request.buildId, "scan")')
    publish = pipeline.index('self._stage(request.buildId, "publish")')
    verify = pipeline.index('self._stage(request.buildId, "verify")')

    assert scan < publish < verify
    assert "type=oci,dest=" in pipeline
    assert "--preserve-digests" in pipeline
    assert "source_digest != published_digest" in pipeline
    assert "--tls-verify=false" not in pipeline
    assert "--insecure" not in pipeline


def test_build_agent_firewall_is_narrow_and_not_gateway_exposed() -> None:
    firewall = (ROLE / "templates/nftables.conf.j2").read_text()
    telemetry = (ROOT / "cluster/observability/opentelemetry-collector.yaml").read_text()

    assert "groups['control_planes']" in firewall
    assert "tcp dport {{ lab.build_agent.port }} accept" in firewall
    assert "nodePort: 30418" in telemetry
    assert "externalTrafficPolicy: Local" in telemetry
    assert "HTTPRoute" not in telemetry


def test_private_keys_and_credentials_are_not_embedded() -> None:
    tasks = (ROLE / "tasks/main.yaml").read_text()
    environment = (ROLE / "templates/build-agent.env.j2").read_text()

    assert "platform-api-build-agent-client.key" not in tasks
    assert "lab-root-ca.key" not in tasks
    assert "no_log: true" in tasks
    assert "PASSWORD" not in environment.upper()
