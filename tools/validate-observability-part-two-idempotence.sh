#!/bin/sh
set -eu

if [ "$#" -ne 13 ]; then
    echo "usage: $0 HELM KUBECTL KUBECONFIG LOKI_VERSION LOKI_CHART_VERSION LOKI_VALUES LOKI_CHART TEMPO_VERSION TEMPO_CHART_VERSION TEMPO_VALUES TEMPO_CHART MANIFESTS NAMESPACE" >&2
    exit 2
fi

helm_binary=$1
kubectl_binary=$2
kubeconfig=$3
namespace=${13}

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

revision() {
    "$helm_binary" --kubeconfig "$kubeconfig" history "$1" --namespace "$namespace" \
        --output json | uv run python -c 'import json,sys; print(json.load(sys.stdin)[-1]["revision"])'
}

loki_revision_before=$(revision loki)
tempo_revision_before=$(revision tempo)
loki_pvc_before=$(kubectl_cmd --namespace "$namespace" get pvc storage-loki-0 -o jsonpath='{.metadata.uid}')
tempo_pvc_before=$(kubectl_cmd --namespace "$namespace" get pvc storage-tempo-0 -o jsonpath='{.metadata.uid}')
collector_generation_before=$(kubectl_cmd --namespace "$namespace" get daemonset opentelemetry-collector -o jsonpath='{.metadata.generation}')

sh tools/install-observability-part-two.sh "$@" >/dev/null

test "$loki_revision_before" = "$(revision loki)"
test "$tempo_revision_before" = "$(revision tempo)"
test "$loki_pvc_before" = "$(kubectl_cmd --namespace "$namespace" get pvc storage-loki-0 -o jsonpath='{.metadata.uid}')"
test "$tempo_pvc_before" = "$(kubectl_cmd --namespace "$namespace" get pvc storage-tempo-0 -o jsonpath='{.metadata.uid}')"
test "$collector_generation_before" = "$(kubectl_cmd --namespace "$namespace" get daemonset opentelemetry-collector -o jsonpath='{.metadata.generation}')"

echo "Part Two reconciliation is idempotent: Helm revisions, PVC identities and Collector generation are unchanged."
