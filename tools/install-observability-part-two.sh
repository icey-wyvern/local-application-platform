#!/bin/sh
set -eu

if [ "$#" -ne 13 ]; then
    echo "usage: $0 HELM KUBECTL KUBECONFIG LOKI_VERSION LOKI_CHART_VERSION LOKI_VALUES LOKI_CHART TEMPO_VERSION TEMPO_CHART_VERSION TEMPO_VALUES TEMPO_CHART MANIFESTS NAMESPACE" >&2
    exit 2
fi

helm_binary=$1
kubectl_binary=$2
kubeconfig=$3
loki_version=$4
loki_chart_version=$5
loki_values=$6
loki_chart=$7
tempo_version=$8
tempo_chart_version=$9
tempo_values=${10}
tempo_chart=${11}
manifests=${12}
namespace=${13}
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
uv_binary=$(command -v uv 2>/dev/null || printf '%s/.local/bin/uv' "$HOME")

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

reconcile_release() {
    release=$1
    chart_version=$2
    values=$3
    chart=$4
    current_values="$temporary_directory/${release}-values.yaml"

    release_matches=false
    if "$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$namespace" \
        --filter "^${release}$" --output json | grep -q "\"chart\":\"${release}-${chart_version}\""; then
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
        echo "$release chart ${chart_version} already matches generated configuration."
    else
        "$helm_binary" --kubeconfig "$kubeconfig" upgrade --install "$release" "$chart" \
            --namespace "$namespace" --values "$values" --rollback-on-failure --wait --timeout 15m
    fi
}

kubectl_cmd apply --server-side --field-manager=lap-kustomize \
    -f "$manifests/namespace.yaml" >/dev/null
reconcile_release loki "$loki_chart_version" "$loki_values" "$loki_chart"
reconcile_release tempo "$tempo_chart_version" "$tempo_values" "$tempo_chart"
kubectl_cmd apply --server-side --field-manager=lap-kustomize --kustomize "$manifests" >/dev/null

kubectl_cmd --namespace "$namespace" rollout status statefulset/loki --timeout=15m
kubectl_cmd --namespace "$namespace" rollout status statefulset/tempo --timeout=15m
kubectl_cmd --namespace "$namespace" rollout status daemonset/opentelemetry-collector --timeout=15m
test "$(kubectl_cmd --namespace "$namespace" get daemonset opentelemetry-collector -o jsonpath='{.status.numberReady}')" = 6
test "$(kubectl_cmd --namespace "$namespace" get pod loki-0 -o jsonpath='{.spec.containers[0].image}')" = "docker.io/grafana/loki:${loki_version}"
test "$(kubectl_cmd --namespace "$namespace" get pod tempo-0 -o jsonpath='{.spec.containers[0].image}')" = "docker.io/grafana/tempo:${tempo_version}"

echo "Loki ${loki_version}, Tempo ${tempo_version}, and the OpenTelemetry Collector are installed and healthy."
