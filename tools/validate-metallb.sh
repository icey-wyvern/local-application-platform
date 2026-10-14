#!/bin/sh
set -eu

if [ "$#" -lt 8 ] || [ "$#" -gt 9 ]; then
    echo "usage: $0 HELM KUBECONFIG VERSION POOL_START POOL_END RESERVED_IP TEST_IP API_VIP [--failure]" >&2
    exit 2
fi

helm_binary=$1
kubeconfig=$2
version=$3
pool_start=$4
pool_end=$5
reserved_ip=$6
test_ip=$7
api_vip=$8
mode=${9:-normal}
namespace=metallb-validation
metallb_namespace=metallb-system
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(dirname "$script_directory")
manifests="$repo_root/cluster/validation/metallb"
kubectl_binary="$HOME/.local/bin/kubectl"
leader_node=
replacement_node=
previous_exclusion=
leader_was_modified=false

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

remove_validation_resources() {
    kubectl_cmd delete --kustomize "$manifests" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    if kubectl_cmd get namespace "$namespace" >/dev/null 2>&1; then
        kubectl_cmd wait --for=delete "namespace/$namespace" --timeout=2m >/dev/null
    fi
}

restore_leader() {
    if [ "$leader_was_modified" != true ]; then
        return
    fi
    if [ -n "$previous_exclusion" ]; then
        kubectl_cmd label node "$leader_node" \
            "node.kubernetes.io/exclude-from-external-load-balancers=$previous_exclusion" --overwrite >/dev/null
    else
        kubectl_cmd label node "$leader_node" \
            node.kubernetes.io/exclude-from-external-load-balancers- >/dev/null
    fi
    leader_was_modified=false
}

cleanup() {
    restore_leader || true
    remove_validation_resources || true
}
trap cleanup EXIT HUP INT TERM

release_chart=$(
    "$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$metallb_namespace" \
        --filter '^metallb$' --output json
)
printf '%s' "$release_chart" | grep -q "\"chart\":\"metallb-${version}\""

kubectl_cmd --namespace "$metallb_namespace" rollout status deployment/metallb-controller --timeout=5m
kubectl_cmd --namespace "$metallb_namespace" rollout status daemonset/metallb-speaker --timeout=5m
test "$(kubectl_cmd --namespace "$metallb_namespace" get deployment metallb-controller -o jsonpath='{.status.availableReplicas}')" = "1"
test "$(kubectl_cmd --namespace "$metallb_namespace" get daemonset metallb-speaker -o jsonpath='{.status.numberReady}')" = "6"
test "$(kubectl_cmd get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')" = "6"
test "$(kubectl_cmd --namespace kube-system get daemonset cilium -o jsonpath='{.status.numberReady}')" = "6"
test "$(kubectl_cmd --namespace kube-system get daemonset kube-proxy -o jsonpath='{.status.numberReady}')" = "6"
test "$(kubectl_cmd get --raw=/readyz)" = "ok"
test "$(kubectl_cmd config view --minify -o jsonpath='{.clusters[0].cluster.server}')" = "https://${api_vip}:6443"
test "$pool_start" = "$reserved_ip"
uv run python -c 'import ipaddress,sys; assert ipaddress.ip_address(sys.argv[2]) == ipaddress.ip_address(sys.argv[1]) + 1' \
    "$pool_start" "$test_ip"

kubectl_cmd --namespace "$metallb_namespace" get ipaddresspools.metallb.io -o json |
    uv run python -c \
        'import json,sys; data=json.load(sys.stdin); pools={i["metadata"]["name"]:(i["spec"]["addresses"],i["spec"]["autoAssign"]) for i in data["items"]}; assert pools=={"envoy-gateway-reserved":([f"{sys.argv[1]}/32"],False),"lab-general":([f"{sys.argv[2]}-{sys.argv[3]}"],True)}' \
        "$reserved_ip" "$test_ip" "$pool_end"
test "$(kubectl_cmd --namespace "$metallb_namespace" get l2advertisements.metallb.io lab -o jsonpath='{.spec.ipAddressPools[*]}')" = "envoy-gateway-reserved lab-general"
test -z "$(kubectl_cmd get bgpadvertisements.metallb.io --all-namespaces --no-headers 2>/dev/null || true)"
test -z "$(kubectl_cmd get bgppeers.metallb.io --all-namespaces --no-headers 2>/dev/null || true)"

remove_validation_resources
kubectl_cmd apply --kustomize "$manifests" >/dev/null
kubectl_cmd --namespace "$namespace" rollout status deployment/metallb-test --timeout=3m
kubectl_cmd --namespace "$namespace" wait --for=condition=Ready pod/metallb-client --timeout=3m

test "$(kubectl_cmd --namespace "$namespace" get service metallb-test -o jsonpath='{.status.loadBalancer.ingress[0].ip}')" = "$test_ip"
test "$(kubectl_cmd --namespace "$namespace" get endpointslice -l kubernetes.io/service-name=metallb-test -o jsonpath='{range .items[*].endpoints[*]}{.conditions.ready}{"\n"}{end}' | grep -c '^true$')" = "2"
kubectl_cmd --namespace "$namespace" exec metallb-client -- \
    wget -qO- http://metallb-test:8080/ | grep -Eq '^metallb-test-[a-z0-9-]+$'
kubectl_cmd --namespace "$namespace" exec metallb-client -- \
    nslookup metallb-test.metallb-validation.svc.cluster.local >/dev/null

for attempt in $(seq 1 30); do
    leader_node=$(kubectl_cmd --namespace "$metallb_namespace" get servicel2statuses.metallb.io \
        -l metallb.io/service-name=metallb-test,metallb.io/service-namespace="$namespace" \
        -o jsonpath='{.items[0].status.node}' 2>/dev/null || true)
    [ -n "$leader_node" ] && break
    sleep 1
done
test -n "$leader_node"
kubectl_cmd get node "$leader_node" >/dev/null

wsl_body=$(wget -qO- "http://${test_ip}:8080/")
printf '%s\n' "$wsl_body" | grep -Eq '^metallb-test-[a-z0-9-]+$'
windows_body=$(powershell.exe -NoProfile -Command \
    "\$response = Invoke-WebRequest -UseBasicParsing -Uri 'http://${test_ip}:8080/' -TimeoutSec 10; if (\$response.StatusCode -ne 200) { throw 'unexpected status' }; \$response.Content")
printf '%s' "$windows_body" | tr -d '\r' | grep -Eq '^metallb-test-[a-z0-9-]+'
powershell.exe -NoProfile -Command \
    "\$neighbor = Get-NetNeighbor -AddressFamily IPv4 -IPAddress '${test_ip}' -ErrorAction Stop; if (\$neighbor.State -eq 'Unreachable' -or \$neighbor.LinkLayerAddress -eq '00-00-00-00-00-00') { throw 'MetalLB address is not present in the lab neighbor table' }" >/dev/null

if [ "$mode" = "--failure" ]; then
    previous_exclusion=$(kubectl_cmd get node "$leader_node" -o jsonpath='{.metadata.labels.node\.kubernetes\.io/exclude-from-external-load-balancers}')
    kubectl_cmd label node "$leader_node" node.kubernetes.io/exclude-from-external-load-balancers=true --overwrite >/dev/null
    leader_was_modified=true
    for attempt in $(seq 1 60); do
        replacement_node=$(kubectl_cmd --namespace "$metallb_namespace" get servicel2statuses.metallb.io \
            -l metallb.io/service-name=metallb-test,metallb.io/service-namespace="$namespace" \
            -o jsonpath='{range .items[*]}{.status.node}{"\n"}{end}' 2>/dev/null |
            grep -v "^${leader_node}$" | head -1 || true)
        [ -n "$replacement_node" ] && break
        sleep 1
    done
    test -n "$replacement_node"
    echo "MetalLB announcer moved from ${leader_node} to ${replacement_node}."
    wget -qO- "http://${test_ip}:8080/" | grep -Eq '^metallb-test-[a-z0-9-]+$'
    powershell.exe -NoProfile -Command \
        "\$response = Invoke-WebRequest -UseBasicParsing -Uri 'http://${test_ip}:8080/' -TimeoutSec 10; if (\$response.StatusCode -ne 200) { throw 'unexpected status' }" >/dev/null
    restore_leader
    kubectl_cmd --namespace "$metallb_namespace" rollout status daemonset/metallb-speaker --timeout=3m
    test "$(kubectl_cmd --namespace "$metallb_namespace" get daemonset metallb-speaker -o jsonpath='{.status.numberReady}')" = "6"
fi

remove_validation_resources
test -z "$(kubectl_cmd get services --all-namespaces -o jsonpath='{range .items[*].status.loadBalancer.ingress[*]}{.ip}{"\n"}{end}' | grep -E "^(${reserved_ip}|${test_ip})$" || true)"
test -z "$(kubectl_cmd --namespace "$metallb_namespace" get servicel2statuses.metallb.io -l metallb.io/service-name=metallb-test --no-headers 2>/dev/null || true)"
test "$(kubectl_cmd --namespace "$metallb_namespace" get ipaddresspool envoy-gateway-reserved -o jsonpath='{.status.assignedIPv4}')" = "0"
test "$(kubectl_cmd --namespace "$metallb_namespace" get ipaddresspool lab-general -o jsonpath='{.status.assignedIPv4}')" = "0"
test "$(kubectl_cmd --namespace "$metallb_namespace" get daemonset metallb-speaker -o jsonpath='{.status.numberReady}')" = "6"
test "$(kubectl_cmd --namespace "$metallb_namespace" get deployment metallb-controller -o jsonpath='{.status.availableReplicas}')" = "1"

echo "MetalLB validation passed (${mode#--}); initial announcer: ${leader_node}."
