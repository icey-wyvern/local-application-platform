#!/bin/sh
set -eu

if [ "$#" -ne 5 ]; then
    echo "usage: $0 HELM KUBECONFIG VERSION VALUES CHART" >&2
    exit 2
fi

helm_binary=$1
kubeconfig=$2
version=$3
values=$4
chart=$5
namespace=cert-manager
release=cert-manager
kubectl_binary="$HOME/.local/bin/kubectl"
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
current_values="$temporary_directory/current-values.yaml"

release_matches=false
if "$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$namespace" \
    --filter '^cert-manager$' --output json | grep -q "\"chart\":\"cert-manager-v${version}\""; then
    release_matches=true
    "$helm_binary" --kubeconfig "$kubeconfig" get values "$release" \
        --namespace "$namespace" --output yaml >"$current_values"
fi

values_match=false
if [ "$release_matches" = true ] && uv run --package lap-config python -c \
    'import pathlib, sys, yaml; sys.exit(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) != yaml.safe_load(pathlib.Path(sys.argv[2]).read_text()))' \
    "$values" "$current_values"; then
    values_match=true
fi

if [ "$release_matches" = true ] && [ "$values_match" = true ]; then
    echo "cert-manager ${version} already matches the generated Helm configuration."
else
    "$helm_binary" --kubeconfig "$kubeconfig" upgrade --install "$release" "$chart" \
        --namespace "$namespace" --create-namespace --values "$values" \
        --rollback-on-failure --wait --timeout 10m
fi

for deployment in cert-manager cert-manager-webhook cert-manager-cainjector; do
    "$kubectl_binary" --kubeconfig "$kubeconfig" --namespace "$namespace" \
        rollout status "deployment/$deployment" --timeout=5m
done
