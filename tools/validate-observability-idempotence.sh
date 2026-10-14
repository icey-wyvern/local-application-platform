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
namespace=$9
admin_password=${10}

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

revision_before=$("$helm_binary" --kubeconfig "$kubeconfig" history kube-prometheus-stack \
    --namespace "$namespace" --output json | uv run python -c \
    'import json,sys; print(json.load(sys.stdin)[-1]["revision"])')
credential_before=$(cksum "$admin_password")
grafana_pvc_before=$(kubectl_cmd --namespace "$namespace" get pvc \
    -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.uid}')
prometheus_pvc_before=$(kubectl_cmd --namespace "$namespace" get pvc \
    -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.uid}')

sh tools/install-observability.sh "$@" >/dev/null

revision_after=$("$helm_binary" --kubeconfig "$kubeconfig" history kube-prometheus-stack \
    --namespace "$namespace" --output json | uv run python -c \
    'import json,sys; print(json.load(sys.stdin)[-1]["revision"])')
test "$revision_before" = "$revision_after"
test "$credential_before" = "$(cksum "$admin_password")"
test "$grafana_pvc_before" = "$(kubectl_cmd --namespace "$namespace" get pvc \
    -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.uid}')"
test "$prometheus_pvc_before" = "$(kubectl_cmd --namespace "$namespace" get pvc \
    -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.uid}')"

echo "Observability reconciliation is idempotent: Helm revision, PVCs and credentials are unchanged."
