#!/bin/sh
set -eu

if [ "$#" -lt 6 ] || [ "$#" -gt 7 ]; then
    echo "usage: $0 HELM KUBECONFIG VERSION POD_CIDR NODE_CIDR_MASK API_VIP [--failure]" >&2
    exit 2
fi

helm_binary=$1
kubeconfig=$2
version=$3
pod_cidr=$4
node_cidr_mask=$5
api_vip=$6
mode=${7:-normal}
namespace=lap-networking-test
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(dirname "$script_directory")
manifests="$repo_root/cluster/validation/networking"
kubectl_binary="$HOME/.local/bin/kubectl"
temporary_directory=$(mktemp -d)

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

remove_validation_resources() {
    kubectl_cmd delete --kustomize "$manifests" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    if kubectl_cmd get namespace "$namespace" >/dev/null 2>&1; then
        kubectl_cmd wait --for=delete "namespace/$namespace" --timeout=2m >/dev/null
    fi
}

cleanup() {
    remove_validation_resources || true
    rm -rf "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

release_chart=$(
    "$helm_binary" --kubeconfig "$kubeconfig" list --namespace kube-system \
        --filter '^cilium$' --output json
)
printf '%s' "$release_chart" | grep -q "\"chart\":\"cilium-${version}\""

kubectl_cmd wait --for=condition=Ready node --all --timeout=5m
kubectl_cmd --namespace kube-system rollout status daemonset/cilium --timeout=5m
kubectl_cmd --namespace kube-system rollout status deployment/cilium-operator --timeout=5m
kubectl_cmd --namespace kube-system rollout status deployment/coredns --timeout=5m
kubectl_cmd --namespace kube-system rollout status daemonset/kube-proxy --timeout=5m

test "$(kubectl_cmd --namespace kube-system get daemonset cilium -o jsonpath='{.status.numberReady}')" = "6"
test "$(kubectl_cmd --namespace kube-system get daemonset kube-proxy -o jsonpath='{.status.numberReady}')" = "6"
test "$(kubectl_cmd --namespace kube-system get deployment cilium-operator -o jsonpath='{.status.availableReplicas}')" = "2"
test "$(kubectl_cmd --namespace kube-system get deployment coredns -o jsonpath='{.status.availableReplicas}')" = "2"

test "$(kubectl_cmd --namespace kube-system get configmap cilium-config -o jsonpath='{.data.ipam}')" = "kubernetes"
test "$(kubectl_cmd --namespace kube-system get configmap cilium-config -o jsonpath='{.data.kube-proxy-replacement}')" = "false"
test "$(kubectl_cmd --namespace kube-system get configmap cilium-config -o jsonpath='{.data.routing-mode}')" = "tunnel"
test "$(kubectl_cmd --namespace kube-system get configmap cilium-config -o jsonpath='{.data.tunnel-protocol}')" = "vxlan"
test "$(kubectl_cmd config view --minify -o jsonpath='{.clusters[0].cluster.server}')" = "https://${api_vip}:6443"

kubectl_cmd get nodes -o json >"$temporary_directory/nodes.json"
uv run python -c \
    'import ipaddress,json,pathlib,sys; data=json.loads(pathlib.Path(sys.argv[1]).read_text()); cluster=ipaddress.ip_network(sys.argv[2]); mask=int(sys.argv[3]); cidrs=[ipaddress.ip_network(item["spec"]["podCIDR"]) for item in data["items"]]; assert len(cidrs)==6 and len(set(cidrs))==6; assert all(c.prefixlen==mask and c.subnet_of(cluster) for c in cidrs)' \
    "$temporary_directory/nodes.json" "$pod_cidr" "$node_cidr_mask"

for pod in $(kubectl_cmd --namespace kube-system get pods --selector k8s-app=cilium -o name); do
    kubectl_cmd --namespace kube-system exec "$pod" -- cilium-dbg status --brief >/dev/null
done

remove_validation_resources
kubectl_cmd apply --kustomize "$manifests" >/dev/null
kubectl_cmd --namespace "$namespace" wait --for=condition=Ready pod --all --timeout=3m

server_one_ip=$(kubectl_cmd --namespace "$namespace" get pod net-server-worker-1 -o jsonpath='{.status.podIP}')
server_two_ip=$(kubectl_cmd --namespace "$namespace" get pod net-server-worker-2 -o jsonpath='{.status.podIP}')
client_ip=$(kubectl_cmd --namespace "$namespace" get pod net-client-worker-3 -o jsonpath='{.status.podIP}')

uv run python -c \
    'import ipaddress,sys; pairs=zip(sys.argv[1::2],sys.argv[2::2],strict=True); assert all(ipaddress.ip_address(ip) in ipaddress.ip_network(cidr) for ip,cidr in pairs)' \
    "$server_one_ip" "$(kubectl_cmd get node worker-1 -o jsonpath='{.spec.podCIDR}')" \
    "$server_two_ip" "$(kubectl_cmd get node worker-2 -o jsonpath='{.spec.podCIDR}')" \
    "$client_ip" "$(kubectl_cmd get node worker-3 -o jsonpath='{.spec.podCIDR}')"

kubectl_cmd --namespace "$namespace" exec net-client-worker-3 -- \
    wget -qO- "http://$server_one_ip:8080" | grep -qx worker-1
kubectl_cmd --namespace "$namespace" exec net-client-worker-3 -- \
    wget -qO- "http://$server_two_ip:8080" | grep -qx worker-2
kubectl_cmd --namespace "$namespace" exec net-client-worker-3 -- \
    wget -qO- http://net-echo:8080 | grep -Eq '^worker-[12]$'
kubectl_cmd --namespace "$namespace" exec net-client-worker-3 -- \
    nslookup kubernetes.default.svc.cluster.local >/dev/null
kubectl_cmd --namespace "$namespace" exec net-client-worker-3 -- \
    nslookup example.com >/dev/null
kubectl_cmd --namespace "$namespace" exec net-client-worker-3 -- \
    wget -qO- http://example.com/ >/dev/null
test "$(kubectl_cmd get --raw=/readyz)" = "ok"

if [ "$mode" = "--failure" ]; then
    old_pod=$(kubectl_cmd --namespace kube-system get pods --selector k8s-app=cilium \
        --field-selector spec.nodeName=worker-1 -o jsonpath='{.items[0].metadata.name}')
    old_uid=$(kubectl_cmd --namespace kube-system get pod "$old_pod" -o jsonpath='{.metadata.uid}')
    kubectl_cmd --namespace kube-system delete pod "$old_pod" --wait=false >/dev/null
    kubectl_cmd --namespace kube-system wait --for=delete "pod/$old_pod" --timeout=2m
    kubectl_cmd --namespace kube-system rollout status daemonset/cilium --timeout=5m
    kubectl_cmd wait --for=condition=Ready node/worker-1 --timeout=3m
    new_pod=$(kubectl_cmd --namespace kube-system get pods --selector k8s-app=cilium \
        --field-selector spec.nodeName=worker-1 -o jsonpath='{.items[0].metadata.name}')
    new_uid=$(kubectl_cmd --namespace kube-system get pod "$new_pod" -o jsonpath='{.metadata.uid}')
    test "$new_uid" != "$old_uid"
    kubectl_cmd --namespace kube-system exec "$new_pod" -- cilium-dbg status --brief >/dev/null
    kubectl_cmd --namespace "$namespace" exec net-client-worker-3 -- \
        wget -qO- "http://$server_one_ip:8080" | grep -qx worker-1
    kubectl_cmd --namespace "$namespace" exec net-client-worker-3 -- \
        nslookup kubernetes.default.svc.cluster.local >/dev/null
fi

echo "Cilium networking validation passed (${mode#--})."
