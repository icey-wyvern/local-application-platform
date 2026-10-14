#!/bin/sh
set -eu

if [ "$#" -ne 10 ]; then
    echo "usage: $0 HELM KUBECTL KUBECONFIG VERSION VALUES CHART MANIFESTS GATEWAY_MANIFESTS NAMESPACE ADMIN_PASSWORD" >&2
    exit 2
fi

helm_binary=$1
kubectl_binary=$2
kubeconfig=$3
version=$4
values=$5
chart=$6
manifests=$7
gateway_manifests=$8
namespace=$9
admin_password=${10}
release=kube-prometheus-stack
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
current_values="$temporary_directory/current-values.yaml"
uv_binary=$(command -v uv 2>/dev/null || printf '%s/.local/bin/uv' "$HOME")

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

mkdir -p "$(dirname "$admin_password")"
chmod 700 "$(dirname "$admin_password")"
if [ -e "$admin_password" ]; then
    test -s "$admin_password" || {
        echo "refusing to replace empty existing Grafana credential file" >&2
        exit 1
    }
else
    umask 077
    openssl rand -hex 32 | tr -d '\n' >"$admin_password"
fi
chmod 600 "$admin_password"

kubectl_cmd apply --server-side --field-manager=lap-kustomize \
    -f "$manifests/namespace.yaml" >/dev/null
kubectl_cmd --namespace "$namespace" create secret generic grafana-administrator \
    --from-literal=admin-user=admin --from-file=admin-password="$admin_password" \
    --dry-run=client -o yaml |
    kubectl_cmd apply --server-side --field-manager=lap-secret-bootstrap -f - >/dev/null

release_matches=false
if "$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$namespace" \
    --filter '^kube-prometheus-stack$' --output json |
    grep -q "\"chart\":\"kube-prometheus-stack-${version}\""; then
    release_matches=true
    "$helm_binary" --kubeconfig "$kubeconfig" get values "$release" \
        --namespace "$namespace" --output yaml >"$current_values"
fi

values_match=false
if [ "$release_matches" = true ] && "$uv_binary" run --package lap-config python -c \
    'import pathlib,sys,yaml; sys.exit(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) != yaml.safe_load(pathlib.Path(sys.argv[2]).read_text()))' \
    "$values" "$current_values"; then
    values_match=true
fi

if [ "$release_matches" = true ] && [ "$values_match" = true ]; then
    echo "kube-prometheus-stack ${version} already matches the generated Helm configuration."
else
    "$helm_binary" --kubeconfig "$kubeconfig" upgrade --install "$release" "$chart" \
        --namespace "$namespace" --values "$values" \
        --rollback-on-failure --wait --timeout 15m
fi

kubectl_cmd apply --server-side --field-manager=lap-kustomize \
    --kustomize "$manifests" >/dev/null
kubectl_cmd apply --server-side --field-manager=lap-kustomize \
    --kustomize "$gateway_manifests" >/dev/null

kubectl_cmd --namespace envoy-gateway-system wait --for=condition=Ready \
    certificate/grafana --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status \
    deployment/kube-prometheus-stack-operator --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status \
    deployment/kube-prometheus-stack-kube-state-metrics --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status \
    statefulset/kube-prometheus-stack-grafana --timeout=10m
kubectl_cmd --namespace "$namespace" rollout status \
    statefulset/prometheus-kube-prometheus-stack-prometheus --timeout=10m
kubectl_cmd --namespace "$namespace" rollout status \
    statefulset/alertmanager-kube-prometheus-stack-alertmanager --timeout=10m
kubectl_cmd --namespace "$namespace" rollout status \
    daemonset/kube-prometheus-stack-prometheus-node-exporter --timeout=10m

echo "kube-prometheus-stack ${version} is installed and healthy."
