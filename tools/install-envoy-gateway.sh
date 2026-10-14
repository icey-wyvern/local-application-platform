#!/bin/sh
set -eu

if [ "$#" -ne 7 ]; then
    echo "usage: $0 HELM KUBECTL KUBECONFIG VERSION CRD_VALUES VALUES CONFIGURATION" >&2
    exit 2
fi

helm_binary=$1
kubectl_binary=$2
kubeconfig=$3
version=$4
crd_values=$5
values=$6
configuration=$7
namespace=envoy-gateway-system
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(dirname "$script_directory")
crd_chart="$repo_root/.local-tools/charts/envoy-gateway/${version}/gateway-crds-helm-v${version}.tgz"
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

release_matches() {
    release=$1
    chart=$2
    wanted_values=$3
    current_values="$temporary_directory/${release}.yaml"
    if ! "$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$namespace" \
        --filter "^${release}$" --output json | grep -q "\"chart\":\"${chart}-v${version}\""; then
        return 1
    fi
    "$helm_binary" --kubeconfig "$kubeconfig" get values "$release" \
        --namespace "$namespace" --output yaml >"$current_values"
    uv run --package lap-config python -c \
        'import pathlib, sys, yaml; sys.exit(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) != yaml.safe_load(pathlib.Path(sys.argv[2]).read_text()))' \
        "$wanted_values" "$current_values"
}

# The chart documents template + server-side apply because Helm release objects
# containing these CRDs exceed Kubernetes' one-megabyte object limit.
"$helm_binary" template envoy-gateway-crds \
    "$crd_chart" \
    --namespace "$namespace" --values "$crd_values" |
    "$kubectl_binary" --kubeconfig "$kubeconfig" apply --server-side \
        --field-manager=envoy-gateway-crds --filename -

if release_matches envoy-gateway gateway-helm "$values"; then
    echo "Envoy Gateway ${version} already matches the generated Helm configuration."
else
    "$helm_binary" --kubeconfig "$kubeconfig" upgrade --install envoy-gateway \
        oci://docker.io/envoyproxy/gateway-helm \
        --version "v${version}" --namespace "$namespace" --create-namespace \
        --values "$values" --rollback-on-failure --wait --timeout 10m
fi

"$kubectl_binary" --kubeconfig "$kubeconfig" --namespace "$namespace" \
    rollout status deployment/envoy-gateway --timeout=5m
"$kubectl_binary" --kubeconfig "$kubeconfig" apply --server-side \
    --field-manager=lap-kustomize --kustomize "$configuration"
