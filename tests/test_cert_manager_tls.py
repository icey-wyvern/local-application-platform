from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[1]


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text()))


def test_pki_hierarchy_and_validity_contract_is_explicit() -> None:
    script = (ROOT / "tools/prepare-lab-pki.ps1").read_text()
    installer = (ROOT / "tools/install-cluster-pki.sh").read_text()
    issuer = _load(ROOT / "cluster/pki/cluster-issuer.yaml")
    certificate = _load(ROOT / "cluster/pki/wildcard-certificate.yaml")

    assert "'-days', '1825'" in script
    assert "'-days', '365'" in script
    assert "basicConstraints = critical,CA:TRUE,pathlen:1" in script
    assert "basicConstraints = critical,CA:TRUE,pathlen:0" in script
    assert "keyCertSign,cRLSign" in script
    assert "refusing silent replacement" in script
    assert issuer["spec"]["ca"]["secretName"] == "cluster-intermediate-ca"
    assert certificate["spec"]["duration"] == "2160h"
    assert certificate["spec"]["renewBefore"] == "360h"
    assert certificate["spec"]["dnsNames"] == ["*.apps.lab.test"]
    assert certificate["spec"]["issuerRef"] == {
        "group": "cert-manager.io",
        "kind": "ClusterIssuer",
        "name": "lap-cluster-intermediate",
    }
    assert '--key="$pending_key"' in installer
    assert "lab-root-ca.key" not in installer


def test_root_private_key_is_host_only_and_gitignored() -> None:
    ignore = (ROOT / ".gitignore").read_text()
    script = (ROOT / "tools/prepare-lab-pki.ps1").read_text()

    assert ".generated/" in ignore
    assert "Set-PrivateAcl -Path $rootKey" in script
    assert "Cert:\\CurrentUser\\Root" in script
    assert "'-addstore', 'Root'" in script


def test_gateway_terminates_tls_and_redirects_http() -> None:
    gateway = _load(ROOT / "cluster/gateway/gateway.yaml")
    redirect = _load(ROOT / "cluster/gateway/http-redirect.yaml")
    listeners = {listener["name"]: listener for listener in gateway["spec"]["listeners"]}

    assert listeners["https"] == {
        "allowedRoutes": {"namespaces": {"from": "All"}},
        "hostname": "*.apps.lab.test",
        "name": "https",
        "port": 443,
        "protocol": "HTTPS",
        "tls": {
            "certificateRefs": [{"group": "", "kind": "Secret", "name": "apps-wildcard-tls"}],
            "mode": "Terminate",
        },
    }
    assert redirect["spec"]["parentRefs"][0]["sectionName"] == "http"
    assert redirect["spec"]["rules"][0]["filters"][0] == {
        "requestRedirect": {"scheme": "https", "statusCode": 308},
        "type": "RequestRedirect",
    }


def test_tls_validation_never_disables_certificate_verification() -> None:
    shell = (ROOT / "tools/validate-cert-manager-tls.sh").read_text()
    windows = (ROOT / "tools/validate-windows-gateway-tls.ps1").read_text()
    ansible = (
        ROOT / "infrastructure/ansible/playbooks/validate-gateway-tls-hosts.yaml"
    ).read_text()

    assert "--insecure" not in shell
    assert "--ssl-no-revoke" not in shell
    assert "--ssl-revoke-best-effort" not in shell
    assert "curl -k" not in shell
    assert "--resolve" in ansible
    assert "--insecure" not in ansible
    assert "--resolve" in shell
    assert "openssl verify" in shell
    assert "delete pod" in shell
    assert "trap cleanup" in shell
    assert "AuthenticateAsClient($Hostname)" in windows
    assert "RemoteCertificateValidationCallback" not in windows


def test_tls_validation_workload_is_temporary_and_worker_separated() -> None:
    validation = ROOT / "cluster/validation/tls"
    deployment = _load(validation / "deployment.yaml")
    route = _load(validation / "route.yaml")

    assert deployment["spec"]["replicas"] == 2
    assert route["spec"]["hostnames"] == ["tls-smoke.apps.lab.test"]
    assert route["spec"]["parentRefs"][0]["sectionName"] == "https"
    assert route["spec"]["parentRefs"][0]["namespace"] == "envoy-gateway-system"


def test_tls_slice_does_not_install_later_services() -> None:
    content = "\n".join(
        path.read_text()
        for path in (
            ROOT / "tools/install-cert-manager.sh",
            ROOT / "tools/install-cluster-pki.sh",
            ROOT / "tools/validate-cert-manager-tls.sh",
            ROOT / "cluster/pki/kustomization.yaml",
        )
    ).lower()
    for component in (
        "longhorn",
        "postgresql",
        "observability",
        "buildkit",
        "platform api",
    ):
        assert component not in content
