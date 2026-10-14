#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
    echo "usage: $0 KUBECTL KUBECONFIG VERSION SHA256" >&2
    exit 2
fi

kubectl_binary=$1
kubeconfig=$2
version=$3
expected_sha256=$4
url="https://github.com/kubernetes-sigs/gateway-api/releases/download/v${version}/standard-install.yaml"
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
manifest="$temporary_directory/standard-install.yaml"

curl --fail --location --silent --show-error "$url" --output "$manifest"
printf '%s  %s\n' "$expected_sha256" "$manifest" | sha256sum --check --status
"$kubectl_binary" --kubeconfig "$kubeconfig" apply --server-side \
    --field-manager=gateway-api --filename "$manifest"

for resource in gatewayclasses gateways httproutes referencegrants; do
    crd="${resource}.gateway.networking.k8s.io"
    test "$("$kubectl_binary" --kubeconfig "$kubeconfig" get crd "$crd" \
        -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version}')" = "v${version}"
    test "$("$kubectl_binary" --kubeconfig "$kubeconfig" get crd "$crd" \
        -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/channel}')" = standard
done

echo "Gateway API ${version} standard CRDs are installed."
