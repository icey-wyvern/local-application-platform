#!/bin/sh
set -eu

if [ "$#" -lt 7 ] || [ "$#" -gt 8 ]; then
    echo "usage: $0 HELM KUBECONFIG VERSION GATEWAY_IP API_VIP HOSTNAME ROOT_CERT [--failure]" >&2
    exit 2
fi

helm_binary=$1
kubeconfig=$2
version=$3
gateway_ip=$4
api_vip=$5
hostname=$6
root_certificate=$7
mode=${8:-normal}
kubectl_binary="$HOME/.local/bin/kubectl"
cert_manager_namespace=cert-manager
gateway_namespace=envoy-gateway-system
validation_namespace=tls-validation
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(dirname "$script_directory")
windows_validation=$(wslpath -w "$repo_root/tools/validate-windows-gateway-tls.ps1")
intermediate_certificate="$repo_root/.generated/pki/cluster-intermediate-ca.crt"
manifests="$repo_root/cluster/validation/tls"
temporary_directory=$(mktemp -d)

kubectl_cmd() { "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"; }
remove_validation_resources() {
    kubectl_cmd delete --kustomize "$manifests" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    if kubectl_cmd get namespace "$validation_namespace" >/dev/null 2>&1; then
        kubectl_cmd wait --for=delete "namespace/$validation_namespace" --timeout=3m >/dev/null || true
    fi
}
cleanup() {
    remove_validation_resources
    rm -rf "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

"$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$cert_manager_namespace" \
    --filter '^cert-manager$' --output json | grep -q "\"chart\":\"cert-manager-v${version}\""
for deployment in cert-manager cert-manager-webhook cert-manager-cainjector; do
    kubectl_cmd --namespace "$cert_manager_namespace" rollout status \
        "deployment/$deployment" --timeout=5m
    test "$(kubectl_cmd --namespace "$cert_manager_namespace" get deployment "$deployment" \
        -o jsonpath='{.status.availableReplicas}')" = 1
done
test "$(kubectl_cmd get crd certificates.cert-manager.io \
    -o jsonpath='{.metadata.labels.app\.kubernetes\.io/version}')" = "v${version}"
kubectl_cmd wait --for=condition=Ready clusterissuer/lap-cluster-intermediate --timeout=3m
kubectl_cmd --namespace "$gateway_namespace" wait \
    --for=condition=Ready certificate/apps-wildcard --timeout=5m
test "$(kubectl_cmd --namespace "$gateway_namespace" get certificate apps-wildcard \
    -o jsonpath='{.spec.duration}')" = 2160h
test "$(kubectl_cmd --namespace "$gateway_namespace" get certificate apps-wildcard \
    -o jsonpath='{.spec.issuerRef.name}')" = lap-cluster-intermediate

test -f "$root_certificate"
test -f "$intermediate_certificate"
openssl verify -CAfile "$root_certificate" "$intermediate_certificate"
openssl x509 -in "$root_certificate" -noout -checkend 157161600
if openssl x509 -in "$root_certificate" -noout -checkend 157852800 >/dev/null; then
    echo "Root CA validity exceeds the intended five-year window." >&2
    exit 1
fi
openssl x509 -in "$intermediate_certificate" -noout -checkend 31449600
if openssl x509 -in "$intermediate_certificate" -noout -checkend 31622400 >/dev/null; then
    echo "Intermediate CA validity exceeds the intended one-year window." >&2
    exit 1
fi
openssl x509 -in "$intermediate_certificate" -noout -text | grep -q 'CA:TRUE, pathlen:0'

kubectl_cmd --namespace "$gateway_namespace" get secret apps-wildcard-tls \
    -o jsonpath='{.data.tls\.crt}' | base64 --decode >"$temporary_directory/leaf-chain.crt"
openssl verify -CAfile "$root_certificate" -untrusted "$intermediate_certificate" \
    "$temporary_directory/leaf-chain.crt"
openssl x509 -in "$temporary_directory/leaf-chain.crt" -noout -ext subjectAltName |
    grep -q 'DNS:\*\.apps\.lab\.test'
openssl x509 -in "$temporary_directory/leaf-chain.crt" -noout -issuer |
    grep -q 'Local Application Platform Cluster Intermediate CA'
openssl x509 -in "$temporary_directory/leaf-chain.crt" -noout -checkend 7689600
if openssl x509 -in "$temporary_directory/leaf-chain.crt" -noout -checkend 7862400 >/dev/null; then
    echo "Leaf certificate validity exceeds the intended 90-day window." >&2
    exit 1
fi
leaf_serial=$(openssl x509 -in "$temporary_directory/leaf-chain.crt" -noout -serial)

kubectl_cmd wait --for=condition=Accepted gatewayclass/lap-envoy --timeout=3m
kubectl_cmd --namespace "$gateway_namespace" wait --for=condition=Accepted gateway/platform --timeout=3m
kubectl_cmd --namespace "$gateway_namespace" wait --for=condition=Programmed gateway/platform --timeout=3m
test "$(kubectl_cmd --namespace "$gateway_namespace" get gateway platform \
    -o jsonpath='{.status.listeners[?(@.name=="https")].conditions[?(@.type=="ResolvedRefs")].status}')" = True
test "$(kubectl_cmd --namespace "$gateway_namespace" get service \
    -l gateway.envoyproxy.io/owning-gateway-name=platform \
    -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}')" = "$gateway_ip"
test "$(kubectl_cmd get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')" = 6
test "$(kubectl_cmd --namespace kube-system get daemonset cilium -o jsonpath='{.status.numberReady}')" = 6
test "$(kubectl_cmd --namespace metallb-system get daemonset metallb-speaker -o jsonpath='{.status.numberReady}')" = 6
kubectl_cmd --namespace kube-system rollout status deployment/coredns --timeout=3m
test "$(kubectl_cmd get --raw=/readyz)" = ok
test "$(kubectl_cmd config view --minify -o jsonpath='{.clusters[0].cluster.server}')" = "https://${api_vip}:6443"

remove_validation_resources
kubectl_cmd apply --kustomize "$manifests" >/dev/null
kubectl_cmd --namespace "$validation_namespace" rollout status deployment/tls-smoke --timeout=5m
for condition in Accepted ResolvedRefs; do
    route_ready=false
    for attempt in $(seq 1 60); do
        if kubectl_cmd --namespace "$validation_namespace" get httproute tls-smoke \
            -o jsonpath='{range .status.parents[*].conditions[*]}{.type}={.status}{"\n"}{end}' |
            grep -qx "${condition}=True"; then
            route_ready=true
            break
        fi
        sleep 1
    done
    test "$route_ready" = true
done
test "$(kubectl_cmd --namespace "$validation_namespace" get pods \
    -l app.kubernetes.io/name=tls-smoke \
    -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u | wc -l)" = 2

python3 "$repo_root/infrastructure/ansible/playbooks/files/query_dns.py" \
    "$api_vip" "$hostname" "$gateway_ip"
wsl_body=$(curl --fail --silent --show-error \
    --resolve "${hostname}:443:${gateway_ip}" "https://${hostname}/")
printf '%s\n' "$wsl_body" | grep -Eq '^tls-smoke-[a-z0-9-]+$'
powershell.exe -NoProfile -ExecutionPolicy Bypass \
    -File "$windows_validation" \
    -Address "$gateway_ip" -DnsServer "$api_vip" -Hostname "$hostname"

redirect_headers=$(curl --silent --show-error --head \
    --resolve "${hostname}:80:${gateway_ip}" "http://${hostname}/")
printf '%s\n' "$redirect_headers" | grep -Eq '^HTTP/[0-9.]+ 308'
printf '%s\n' "$redirect_headers" | tr -d '\r' | grep -iq "^location: https://${hostname}/$"

responses=
for attempt in $(seq 1 20); do
    responses="${responses}$(curl --fail --silent --show-error \
        --resolve "${hostname}:443:${gateway_ip}" "https://${hostname}/")\n"
done
test "$(printf '%b' "$responses" | grep '^tls-smoke-' | sort -u | wc -l)" = 2

(cd "$repo_root/infrastructure/ansible" && \
    ANSIBLE_CONFIG="$repo_root/infrastructure/ansible/ansible.cfg" \
    uv tool run --from ansible-core==2.21.3 ansible-playbook \
        playbooks/validate-gateway-tls-hosts.yaml \
        --private-key ${HOME}/.ssh/lap_lab_ed25519)

if [ "$mode" = --failure ]; then
    controller_pod=$(kubectl_cmd --namespace "$cert_manager_namespace" get pod \
        -l app.kubernetes.io/name=cert-manager,app.kubernetes.io/component=controller \
        -o jsonpath='{.items[0].metadata.name}')
    controller_uid=$(kubectl_cmd --namespace "$cert_manager_namespace" get pod "$controller_pod" \
        -o jsonpath='{.metadata.uid}')
    kubectl_cmd --namespace "$cert_manager_namespace" delete pod "$controller_pod" --wait=false >/dev/null
    curl --fail --silent --show-error --output /dev/null \
        --resolve "${hostname}:443:${gateway_ip}" "https://${hostname}/"
    kubectl_cmd --namespace "$cert_manager_namespace" rollout status deployment/cert-manager --timeout=5m
    kubectl_cmd --namespace "$cert_manager_namespace" wait --for=delete "pod/$controller_pod" --timeout=3m
    test -z "$(kubectl_cmd --namespace "$cert_manager_namespace" get pods \
        -l app.kubernetes.io/name=cert-manager,app.kubernetes.io/component=controller \
        -o jsonpath='{range .items[*]}{.metadata.uid}{"\n"}{end}' | grep -x "$controller_uid" || true)"
    curl --fail --silent --show-error --output /dev/null \
        --resolve "${hostname}:443:${gateway_ip}" "https://${hostname}/"
fi

remove_validation_resources
test -z "$(kubectl_cmd get namespace "$validation_namespace" --ignore-not-found -o name)"
test "$(kubectl_cmd --namespace "$gateway_namespace" get certificate apps-wildcard \
    -o jsonpath='{.status.revision}')" = 1
kubectl_cmd --namespace "$gateway_namespace" get secret apps-wildcard-tls \
    -o jsonpath='{.data.tls\.crt}' | base64 --decode >"$temporary_directory/final-leaf.crt"
test "$(openssl x509 -in "$temporary_directory/final-leaf.crt" -noout -serial)" = "$leaf_serial"
echo "cert-manager TLS validation passed (${mode#--}); trusted HTTPS uses ${gateway_ip}."
