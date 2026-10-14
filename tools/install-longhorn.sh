#!/bin/sh
set -eu

if [ "$#" -lt 7 ]; then
    echo "usage: $0 HELM KUBECTL KUBECONFIG VERSION VALUES KUSTOMIZE_DIR WORKER..." >&2
    exit 2
fi

helm_bin=$1
kubectl_bin=$2
kubeconfig=$3
version=$4
values=$5
kustomize_dir=$6
shift 6
namespace=longhorn-system
release=longhorn
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
current_values="$temporary_directory/current-values.yaml"
uv_binary=$(command -v uv 2>/dev/null || printf '%s/.local/bin/uv' "$HOME")

"$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
    --field-manager=lap-kustomize --kustomize "$kustomize_dir"

for worker in "$@"; do
    "$kubectl_bin" --kubeconfig "$kubeconfig" label node "$worker" \
        node.longhorn.io/create-default-disk=true --overwrite
done

release_matches=false
if "$helm_bin" --kubeconfig "$kubeconfig" list --namespace "$namespace" \
    --filter '^longhorn$' --output json | grep -q "\"chart\":\"longhorn-${version}\""; then
    release_matches=true
    "$helm_bin" --kubeconfig "$kubeconfig" get values "$release" \
        --namespace "$namespace" --output yaml >"$current_values"
fi

values_match=false
if [ "$release_matches" = true ] && "$uv_binary" run --package lap-config python -c \
    'import pathlib, sys, yaml; sys.exit(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) != yaml.safe_load(pathlib.Path(sys.argv[2]).read_text()))' \
    "$values" "$current_values"; then
    values_match=true
fi

if [ "$release_matches" = true ] && [ "$values_match" = true ]; then
    echo "Longhorn ${version} already matches the generated Helm configuration."
else
    "$helm_bin" --kubeconfig "$kubeconfig" upgrade --install "$release" longhorn \
        --repo https://charts.longhorn.io \
        --version "$version" \
        --namespace "$namespace" \
        --values "$values" \
        --rollback-on-failure --wait --timeout 10m
fi
