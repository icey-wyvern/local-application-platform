#!/bin/sh
set -eu

if [ "$#" -lt 7 ] || [ "$#" -gt 8 ]; then
    echo "usage: $0 HELM KUBECONFIG ENVOY_VERSION GATEWAY_API_VERSION GATEWAY_IP API_VIP HOSTNAME [--failure]" >&2
    exit 2
fi

helm_binary=$1
kubeconfig=$2
envoy_version=$3
gateway_api_version=$4
gateway_ip=$5
api_vip=$6
hostname=$7
mode=${8:-normal}
kubectl_binary="$HOME/.local/bin/kubectl"
namespace=envoy-gateway-system
validation_namespace=gateway-validation
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(dirname "$script_directory")
manifests="$repo_root/cluster/validation/gateway"

kubectl_cmd() { "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"; }
cleanup() {
    kubectl_cmd delete --kustomize "$manifests" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    if kubectl_cmd get namespace "$validation_namespace" >/dev/null 2>&1; then
        kubectl_cmd wait --for=delete "namespace/$validation_namespace" --timeout=3m >/dev/null || true
    fi
}
trap cleanup EXIT HUP INT TERM

"$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$namespace" --output json |
    grep -q "\"chart\":\"gateway-helm-v${envoy_version}\""
test "$(kubectl_cmd get crd envoyproxies.gateway.envoyproxy.io -o jsonpath='{.status.conditions[?(@.type=="Established")].status}')" = True
for resource in gatewayclasses gateways httproutes referencegrants; do
    test "$(kubectl_cmd get crd "${resource}.gateway.networking.k8s.io" -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version}')" = "v${gateway_api_version}"
done

kubectl_cmd --namespace "$namespace" rollout status deployment/envoy-gateway --timeout=5m
kubectl_cmd wait --for=condition=Accepted gatewayclass/lap-envoy --timeout=5m
kubectl_cmd --namespace "$namespace" wait --for=condition=Accepted gateway/platform --timeout=5m
kubectl_cmd --namespace "$namespace" wait --for=condition=Programmed gateway/platform --timeout=5m
test "$(kubectl_cmd get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')" = 6
test "$(kubectl_cmd --namespace kube-system get daemonset cilium -o jsonpath='{.status.numberReady}')" = 6
test "$(kubectl_cmd --namespace kube-system get daemonset kube-proxy -o jsonpath='{.status.numberReady}')" = 6
kubectl_cmd --namespace kube-system rollout status deployment/coredns --timeout=3m
test "$(kubectl_cmd get --raw=/readyz)" = ok
test "$(kubectl_cmd config view --minify -o jsonpath='{.clusters[0].cluster.server}')" = "https://${api_vip}:6443"

service=$(kubectl_cmd --namespace "$namespace" get service \
    -l gateway.envoyproxy.io/owning-gateway-name=platform -o jsonpath='{.items[0].metadata.name}')
deployment=$(kubectl_cmd --namespace "$namespace" get deployment \
    -l gateway.envoyproxy.io/owning-gateway-name=platform -o jsonpath='{.items[0].metadata.name}')
test -n "$service"
test -n "$deployment"
kubectl_cmd --namespace "$namespace" rollout status "deployment/$deployment" --timeout=5m
test "$(kubectl_cmd --namespace "$namespace" get deployment "$deployment" -o jsonpath='{.status.availableReplicas}')" = 2
test "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.spec.type}')" = LoadBalancer
test "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.status.loadBalancer.ingress[0].ip}')" = "$gateway_ip"
test "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.metadata.annotations.metallb\.io/address-pool}')" = envoy-gateway-reserved
test "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.metadata.annotations.metallb\.io/loadBalancerIPs}')" = "$gateway_ip"
test "$(kubectl_cmd --namespace "$namespace" get pods -l gateway.envoyproxy.io/owning-gateway-name=platform -o jsonpath='{range .items[*]}{.spec.hostNetwork}{"\n"}{end}' | grep -c '^true$' || true)" = 0
test "$(kubectl_cmd --namespace metallb-system get ipaddresspool envoy-gateway-reserved -o jsonpath='{.status.assignedIPv4}')" = 1
test "$(kubectl_cmd --namespace metallb-system get ipaddresspool lab-general -o jsonpath='{.status.assignedIPv4}')" = 0

cleanup
kubectl_cmd apply --kustomize "$manifests" >/dev/null
kubectl_cmd --namespace "$validation_namespace" rollout status deployment/gateway-smoke --timeout=5m
for condition in Accepted ResolvedRefs; do
    route_ready=false
    for attempt in $(seq 1 60); do
        if kubectl_cmd --namespace "$validation_namespace" get httproute gateway-smoke \
            -o jsonpath='{range .status.parents[*].conditions[*]}{.type}={.status}{"\n"}{end}' |
            grep -qx "${condition}=True"; then
            route_ready=true
            break
        fi
        sleep 1
    done
    test "$route_ready" = true
done
test "$(kubectl_cmd --namespace "$validation_namespace" get pods -l app.kubernetes.io/name=gateway-smoke -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u | wc -l)" = 2
smoke_pod=$(kubectl_cmd --namespace "$validation_namespace" get pod \
    -l app.kubernetes.io/name=gateway-smoke -o jsonpath='{.items[0].metadata.name}')
kubectl_cmd --namespace "$validation_namespace" exec "$smoke_pod" -- \
    nslookup gateway-smoke.gateway-validation.svc.cluster.local >/dev/null
kubectl_cmd --namespace "$validation_namespace" exec "$smoke_pod" -- \
    wget -qO- http://gateway-smoke:8080/ | grep -Eq '^gateway-smoke-[a-z0-9-]+$'

python3 "$repo_root/infrastructure/ansible/playbooks/files/query_dns.py" \
    "$api_vip" "$hostname" "$gateway_ip"
wsl_body=$(wget -qO- --header="Host: ${hostname}" "http://${gateway_ip}/")
printf '%s\n' "$wsl_body" | grep -Eq '^gateway-smoke-[a-z0-9-]+$'
windows_result=$(powershell.exe -NoProfile -Command \
    "\$address = (Resolve-DnsName -Type A '${hostname}' -Server '${api_vip}').IPAddress; if ('${gateway_ip}' -notin \$address) { throw 'unexpected DNS answer' }; \$response = Invoke-WebRequest -UseBasicParsing -Uri 'http://${gateway_ip}/' -Headers @{ Host = '${hostname}' } -TimeoutSec 10; if (\$response.StatusCode -ne 200) { throw 'unexpected status' }; \$response.Content")
printf '%s' "$windows_result" | tr -d '\r' | grep -Eq '^gateway-smoke-[a-z0-9-]+'

responses=
for attempt in $(seq 1 20); do
    responses="${responses}$(wget -qO- --header="Host: ${hostname}" "http://${gateway_ip}/")\n"
done
test "$(printf '%b' "$responses" | grep '^gateway-smoke-' | sort -u | wc -l)" = 2

(cd "$repo_root/infrastructure/ansible" && \
    ANSIBLE_CONFIG="$repo_root/infrastructure/ansible/ansible.cfg" \
    uv tool run --from ansible-core==2.21.3 ansible-playbook \
        playbooks/validate-gateway-routing-hosts.yaml \
        --private-key ${HOME}/.ssh/lap_lab_ed25519)

if [ "$mode" = --failure ]; then
    old_pod=$(kubectl_cmd --namespace "$namespace" get pod \
        -l gateway.envoyproxy.io/owning-gateway-name=platform \
        -o jsonpath='{.items[0].metadata.name}')
    old_uid=$(kubectl_cmd --namespace "$namespace" get pod "$old_pod" -o jsonpath='{.metadata.uid}')
    kubectl_cmd --namespace "$namespace" delete pod "$old_pod" --wait=false >/dev/null
    kubectl_cmd --namespace "$namespace" rollout status "deployment/$deployment" --timeout=5m
    kubectl_cmd --namespace "$namespace" wait --for=delete "pod/$old_pod" --timeout=3m
    test -z "$(kubectl_cmd --namespace "$namespace" get pods -l gateway.envoyproxy.io/owning-gateway-name=platform -o jsonpath='{range .items[*]}{.metadata.uid}{"\n"}{end}' | grep -x "$old_uid" || true)"
    kubectl_cmd --namespace "$namespace" wait --for=condition=Programmed gateway/platform --timeout=2m
    wget -qO- --header="Host: ${hostname}" "http://${gateway_ip}/" | grep -Eq '^gateway-smoke-[a-z0-9-]+$'
fi

cleanup
test -z "$(kubectl_cmd get namespace "$validation_namespace" --ignore-not-found -o name)"
test "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.status.loadBalancer.ingress[0].ip}')" = "$gateway_ip"
test "$(kubectl_cmd --namespace "$namespace" get deployment "$deployment" -o jsonpath='{.status.availableReplicas}')" = 2
echo "Envoy Gateway routing validation passed (${mode#--}); service ${service} owns ${gateway_ip}."
