#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
    echo "usage: $0 KUBECTL KUBECONFIG CONFIGURATION" >&2
    exit 2
fi

kubectl_binary=$1
kubeconfig=$2
configuration=$3
namespace=cert-manager
secret=cluster-intermediate-ca
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(dirname "$script_directory")
pki_directory="$repo_root/.generated/pki"
root_certificate="$pki_directory/lab-root-ca.crt"
intermediate_certificate="$pki_directory/cluster-intermediate-ca.crt"
pending_key="$pki_directory/.cluster-intermediate-ca.key.pending"
pending_request="$pki_directory/.cluster-intermediate-ca.csr.pending"
temporary_directory=$(mktemp -d)

cleanup() {
    rm -f "$pending_key" "$pending_request"
    rm -rf "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

kubectl_cmd() { "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"; }

test -f "$root_certificate"
openssl x509 -in "$root_certificate" -noout -checkend 2592000

if kubectl_cmd --namespace "$namespace" get secret "$secret" >/dev/null 2>&1; then
    test -f "$intermediate_certificate" || {
        echo "The intermediate Secret exists but its host-side public certificate is missing." >&2
        exit 1
    }
    kubectl_cmd --namespace "$namespace" get secret "$secret" \
        -o jsonpath='{.data.tls\.crt}' | base64 --decode >"$temporary_directory/secret-chain.crt"
    kubectl_cmd --namespace "$namespace" get secret "$secret" \
        -o jsonpath='{.data.tls\.key}' | base64 --decode >"$temporary_directory/secret.key"
    chmod 0600 "$temporary_directory/secret.key"
    test "$(openssl x509 -in "$intermediate_certificate" -noout -fingerprint -sha256)" = \
        "$(openssl x509 -in "$temporary_directory/secret-chain.crt" -noout -fingerprint -sha256)"
    test "$(openssl pkey -in "$temporary_directory/secret.key" -pubout 2>/dev/null | sha256sum | cut -d' ' -f1)" = \
        "$(openssl x509 -in "$intermediate_certificate" -pubkey -noout | sha256sum | cut -d' ' -f1)"
    echo "Reusing the existing cluster intermediate CA Secret."
else
    if [ -f "$intermediate_certificate" ]; then
        echo "The intermediate certificate exists without its Kubernetes Secret; refusing silent replacement." >&2
        exit 1
    fi
    (cd "$repo_root" && powershell.exe -NoProfile -ExecutionPolicy Bypass \
        -File tools/prepare-lab-pki.ps1 -CreateIntermediate)
    cat "$intermediate_certificate" "$root_certificate" >"$temporary_directory/intermediate-chain.crt"
    kubectl_cmd --namespace "$namespace" create secret tls "$secret" \
        --cert="$temporary_directory/intermediate-chain.crt" --key="$pending_key" \
        --dry-run=client --output=yaml |
        kubectl_cmd apply --server-side --field-manager=lap-pki --filename=- >/dev/null
    echo "Installed the root-signed cluster intermediate CA Secret."
fi

# OpenSSL notBefore has whole-second precision; allow that boundary to pass
# before a cross-environment verification immediately after first issuance.
sleep 1
openssl verify -CAfile "$root_certificate" "$intermediate_certificate"
kubectl_cmd apply --server-side --field-manager=lap-kustomize --kustomize "$configuration"
kubectl_cmd wait --for=condition=Ready clusterissuer/lap-cluster-intermediate --timeout=3m
kubectl_cmd --namespace envoy-gateway-system wait \
    --for=condition=Ready certificate/apps-wildcard --timeout=5m
