#!/bin/sh
set -eu

if [ "$#" -lt 11 ] || [ "$#" -gt 12 ]; then
    echo "usage: $0 HELM KUBECTL KUBECONFIG LOKI_VERSION LOKI_CHART_VERSION TEMPO_VERSION TEMPO_CHART_VERSION OTEL_VERSION NAMESPACE ADMIN_PASSWORD VALIDATION_MANIFESTS [--failure]" >&2
    exit 2
fi

helm_binary=$1
kubectl_binary=$2
kubeconfig=$3
loki_version=$4
loki_chart_version=$5
tempo_version=$6
tempo_chart_version=$7
otel_version=$8
namespace=$9
admin_password=${10}
validation_manifests=${11}
mode=${12:-normal}
temporary_directory=$(mktemp -d)
forward_pids=
loki_url=http://127.0.0.1:13100
tempo_url=http://127.0.0.1:13200
checkpoint=release-health

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

cleanup() {
    kubectl_cmd delete namespace observability-smoke observability-unauthorized-smoke \
        --ignore-not-found --wait=false >/dev/null 2>&1 || true
    for pid in $forward_pids; do
        kill "$pid" >/dev/null 2>&1 || true
        wait "$pid" >/dev/null 2>&1 || true
    done
    rm -rf "$temporary_directory"
}
finish() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "observability validation failed during $checkpoint (exit $status)" >&2
    fi
    cleanup
}
trap finish EXIT HUP INT TERM

start_forward() {
    service=$1
    mapping=$2
    "$kubectl_binary" --kubeconfig "$kubeconfig" --namespace "$namespace" \
        port-forward "service/$service" "$mapping" \
        >"$temporary_directory/port-forward-${service}.log" 2>&1 &
    forward_pids="$forward_pids $!"
}

wait_http() {
    url=$1
    for attempt in $(seq 1 90); do
        if curl --fail --silent --show-error --output /dev/null "$url" 2>/dev/null; then
            return
        fi
        sleep 1
    done
    echo "endpoint did not become healthy: $url" >&2
    return 1
}

wait_log() {
    marker=$1
    trace_id=$2
    for attempt in $(seq 1 90); do
        if curl --fail --silent --show-error --get \
            --data-urlencode 'query={service_name="telemetry-smoke"}' \
            --data-urlencode "since=1h" "$loki_url/loki/api/v1/query_range" |
            uv run python -c 'import json,sys; d=json.load(sys.stdin); marker,trace=sys.argv[1:]; values=[v[1] for s in d["data"]["result"] for v in s["values"]]; assert any(marker in value and trace in value for value in values)' "$marker" "$trace_id" 2>/dev/null; then
            return
        fi
        sleep 2
    done
    echo "known validation log was not queryable from Loki" >&2
    return 1
}

wait_trace() {
    trace_id=$1
    for attempt in $(seq 1 90); do
        if curl --fail --silent --show-error "$tempo_url/api/traces/$trace_id" |
            uv run python -c 'import json,sys; d=json.load(sys.stdin); spans=[s for b in d["batches"] for scope in b.get("scopeSpans",[]) for s in scope.get("spans",[])]; attrs=[a for b in d["batches"] for a in b.get("resource",{}).get("attributes",[])]; assert any(s.get("name")=="observability-part-two-smoke" for s in spans); assert any(a.get("key")=="service.name" and a.get("value",{}).get("stringValue")=="lap-observability-smoke" for a in attrs)' "$trace_id" 2>/dev/null; then
            return
        fi
        sleep 2
    done
    echo "known validation trace was not queryable from Tempo" >&2
    return 1
}

run_smoke() {
    trace_id=$(openssl rand -hex 16)
    marker="lap-smoke-$(openssl rand -hex 8)"
    kubectl_cmd delete namespace observability-smoke --ignore-not-found --wait=true >/dev/null
    kubectl_cmd apply --server-side --field-manager=lap-validation \
        -f "$validation_manifests/namespace.yaml" >/dev/null
    kubectl_cmd --namespace observability-smoke create configmap telemetry-smoke-identifiers \
        --from-literal="trace-id=$trace_id" --from-literal="log-marker=$marker" \
        --dry-run=client -o yaml |
        kubectl_cmd apply --server-side --field-manager=lap-validation -f - >/dev/null
    kubectl_cmd apply --server-side --field-manager=lap-validation \
        --kustomize "$validation_manifests" >/dev/null
    kubectl_cmd --namespace observability-smoke wait --for=condition=complete \
        job/telemetry-smoke --timeout=5m
    wait_log "$marker" "$trace_id"
    wait_trace "$trace_id"
    printf '%s %s\n' "$trace_id" "$marker"
}

releases=$("$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$namespace" --output json)
printf '%s' "$releases" | grep -q "\"chart\":\"loki-${loki_chart_version}\""
printf '%s' "$releases" | grep -q "\"chart\":\"tempo-${tempo_chart_version}\""
kubectl_cmd --namespace "$namespace" rollout status statefulset/loki --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status statefulset/tempo --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status daemonset/opentelemetry-collector --timeout=5m
test "$(kubectl_cmd --namespace "$namespace" get daemonset opentelemetry-collector -o jsonpath='{.status.numberReady}')" = 6
test "$(kubectl_cmd --namespace "$namespace" get pod loki-0 -o jsonpath='{.spec.containers[0].image}')" = "docker.io/grafana/loki:${loki_version}"
test "$(kubectl_cmd --namespace "$namespace" get pod tempo-0 -o jsonpath='{.spec.containers[0].image}')" = "docker.io/grafana/tempo:${tempo_version}"
test "$(kubectl_cmd --namespace "$namespace" get daemonset opentelemetry-collector -o jsonpath='{.spec.template.spec.containers[0].image}')" = "docker.io/otel/opentelemetry-collector-contrib:${otel_version}"
kubectl_cmd --namespace "$namespace" exec loki-0 -- /usr/bin/loki \
    -verify-config=true -config.file=/etc/loki/config/config.yaml >/dev/null
kubectl_cmd --namespace "$namespace" exec tempo-0 -- /tempo \
    -config.file=/conf/tempo.yaml -config.verify=true >/dev/null
collector_validation_pod=$(kubectl_cmd --namespace "$namespace" get pod \
    -l app.kubernetes.io/name=opentelemetry-collector -o jsonpath='{.items[0].metadata.name}')
kubectl_cmd --namespace "$namespace" exec "$collector_validation_pod" -- \
    /otelcol-contrib validate --config=/conf/config.yaml >/dev/null

checkpoint=service-policy
for service in loki loki-headless loki-memberlist tempo opentelemetry-collector; do
    test "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.spec.type}')" = ClusterIP
    test -z "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.spec.externalIPs}')"
done
kubectl_cmd --namespace "$namespace" get service -o json | uv run python -c 'import json,sys; exposed=[i for i in json.load(sys.stdin)["items"] if i["spec"]["type"] in {"LoadBalancer","NodePort"}]; assert [(i["metadata"]["name"],i["spec"]["type"],i["spec"]["ports"][0].get("nodePort")) for i in exposed]==[("opentelemetry-collector-build-host","NodePort",30418)]'

kubectl_cmd --namespace "$namespace" get configmap loki -o jsonpath='{.data.config\.yaml}' >"$temporary_directory/loki.yaml"
kubectl_cmd --namespace "$namespace" get configmap tempo -o jsonpath='{.data.tempo\.yaml}' >"$temporary_directory/tempo.yaml"
checkpoint=config-policy
grep -Eq 'retention_period: (72h|72h0m0s)' "$temporary_directory/loki.yaml"
grep -Eq 'block_retention: (72h|72h0m0s)' "$temporary_directory/tempo.yaml"
kubectl_cmd --namespace "$namespace" get configmap opentelemetry-collector -o jsonpath='{.data.config\.yaml}' >"$temporary_directory/collector.yaml"
grep -q 'receivers:' "$temporary_directory/collector.yaml"
grep -q 'filelog:' "$temporary_directory/collector.yaml"
grep -q 'otlphttp/loki:' "$temporary_directory/collector.yaml"
grep -q 'otlphttp/tempo:' "$temporary_directory/collector.yaml"

checkpoint=telemetry-endpoints
start_forward loki 13100:3100
start_forward tempo 13200:3200
start_forward kube-prometheus-stack-prometheus 19090:9090
start_forward kube-prometheus-stack-grafana 13000:80
wait_http http://127.0.0.1:13100/ready
wait_http http://127.0.0.1:13200/ready
wait_http http://127.0.0.1:19090/-/ready
wait_http http://127.0.0.1:13000/api/health

curl --fail --silent --show-error http://127.0.0.1:19090/api/v1/targets >"$temporary_directory/targets.json"
uv run python -c 'import json,pathlib; targets=json.loads(pathlib.Path("'$temporary_directory'/targets.json").read_text())["data"]["activeTargets"]; healthy=[t for t in targets if t["health"]=="up"]; required=("loki","tempo","opentelemetry-collector"); assert all(any(name in t["labels"].get("job","") for t in healthy) for name in required), [(t["labels"].get("job"),t["health"],t.get("lastError")) for t in targets]; assert not [t for t in targets if t["health"]=="down"], [(t["labels"].get("job"),t.get("lastError")) for t in targets if t["health"]=="down"]'

printf 'machine 127.0.0.1 login admin password %s\n' "$(cat "$admin_password")" >"$temporary_directory/grafana.netrc"
chmod 600 "$temporary_directory/grafana.netrc"
for datasource in prometheus loki tempo; do
    curl --fail --silent --show-error --netrc-file "$temporary_directory/grafana.netrc" \
        "http://127.0.0.1:13000/api/datasources/uid/${datasource}/health" |
        uv run python -c 'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="OK", d'
done
for dashboard in lap-cluster-health lap-platform-health lap-telemetry-pipelines; do
    curl --fail --silent --show-error --netrc-file "$temporary_directory/grafana.netrc" \
        "http://127.0.0.1:13000/api/dashboards/uid/$dashboard" |
        uv run python -c 'import json,sys; assert json.load(sys.stdin)["dashboard"]["uid"]==sys.argv[1]' "$dashboard"
done

checkpoint=smoke-emission
smoke_result=$(run_smoke)
checkpoint=smoke-correlation
first_trace=$(printf '%s\n' "$smoke_result" | tail -n 1 | awk '{print $1}')
first_marker=$(printf '%s\n' "$smoke_result" | tail -n 1 | awk '{print $2}')

curl --fail --silent --show-error --netrc-file "$temporary_directory/grafana.netrc" --get \
    --data-urlencode 'query={service_name="telemetry-smoke"}' --data-urlencode 'since=1h' \
    http://127.0.0.1:13000/api/datasources/proxy/uid/loki/loki/api/v1/query_range |
    uv run python -c 'import json,sys; d=json.load(sys.stdin); text=" ".join(v[1] for s in d["data"]["result"] for v in s["values"]); assert sys.argv[1] in text and sys.argv[2] in text' "$first_marker" "$first_trace"
curl --fail --silent --show-error --netrc-file "$temporary_directory/grafana.netrc" \
    "http://127.0.0.1:13000/api/datasources/proxy/uid/tempo/api/traces/$first_trace" |
    uv run python -c 'import json,sys; assert json.load(sys.stdin)["batches"]'
curl --fail --silent --show-error --netrc-file "$temporary_directory/grafana.netrc" \
    http://127.0.0.1:13000/api/datasources/uid/loki |
    uv run python -c 'import json,sys; d=json.load(sys.stdin); fields=d["jsonData"]["derivedFields"]; assert any(x["datasourceUid"]=="tempo" for x in fields)'
curl --fail --silent --show-error --netrc-file "$temporary_directory/grafana.netrc" \
    http://127.0.0.1:13000/api/datasources/uid/tempo |
    uv run python -c 'import json,sys; d=json.load(sys.stdin); assert d["jsonData"]["tracesToLogsV2"]["datasourceUid"]=="loki"'

checkpoint=network-policy
kubectl_cmd create namespace observability-unauthorized-smoke >/dev/null
kubectl_cmd --namespace observability-unauthorized-smoke run otlp-denied \
    --image=docker.io/library/busybox:1.37.0 --restart=Never \
    --command -- sh -ec 'if nc -z -w 3 opentelemetry-collector.monitoring.svc.cluster.local 4318; then exit 42; fi' >/dev/null
kubectl_cmd --namespace observability-unauthorized-smoke wait --for=jsonpath='{.status.phase}'=Succeeded \
    pod/otlp-denied --timeout=2m

if [ "$mode" = --failure ]; then
    loki_pvc_uid=$(kubectl_cmd --namespace "$namespace" get pvc storage-loki-0 -o jsonpath='{.metadata.uid}')
    tempo_pvc_uid=$(kubectl_cmd --namespace "$namespace" get pvc storage-tempo-0 -o jsonpath='{.metadata.uid}')
    collector_pod=$(kubectl_cmd --namespace "$namespace" get pod -l app.kubernetes.io/name=opentelemetry-collector -o jsonpath='{.items[0].metadata.name}')
    kubectl_cmd --namespace "$namespace" delete pod loki-0 tempo-0 "$collector_pod" --wait=false >/dev/null
    kubectl_cmd --namespace "$namespace" rollout status statefulset/loki --timeout=10m
    kubectl_cmd --namespace "$namespace" rollout status statefulset/tempo --timeout=10m
    kubectl_cmd --namespace "$namespace" rollout status daemonset/opentelemetry-collector --timeout=10m
    test "$loki_pvc_uid" = "$(kubectl_cmd --namespace "$namespace" get pvc storage-loki-0 -o jsonpath='{.metadata.uid}')"
    test "$tempo_pvc_uid" = "$(kubectl_cmd --namespace "$namespace" get pvc storage-tempo-0 -o jsonpath='{.metadata.uid}')"
    loki_url=http://127.0.0.1:13101
    tempo_url=http://127.0.0.1:13201
    start_forward loki 13101:3100
    start_forward tempo 13201:3200
    wait_http "$loki_url/ready"
    wait_http "$tempo_url/ready"
    wait_log "$first_marker" "$first_trace"
    wait_trace "$first_trace"
    second_result=$(run_smoke)
    second_trace=$(printf '%s\n' "$second_result" | tail -n 1 | awk '{print $1}')
    second_marker=$(printf '%s\n' "$second_result" | tail -n 1 | awk '{print $2}')
    wait_log "$second_marker" "$second_trace"
    wait_trace "$second_trace"
fi

checkpoint=storage-and-alerts
kubectl_cmd --namespace "$namespace" get pvc -o json >"$temporary_directory/pvcs.json"
kubectl_cmd --namespace longhorn-system get volumes.longhorn.io -o json >"$temporary_directory/volumes.json"
kubectl_cmd --namespace longhorn-system get replicas.longhorn.io -o json >"$temporary_directory/replicas.json"
uv run python -c 'import json,pathlib,sys; pvcs=json.loads(pathlib.Path(sys.argv[1]).read_text())["items"]; wanted={"storage-loki-0":"5Gi","storage-tempo-0":"5Gi"}; found={p["metadata"]["name"]:p for p in pvcs if p["metadata"]["name"] in wanted}; assert {n:p["spec"]["resources"]["requests"]["storage"] for n,p in found.items()}==wanted; assert all(p["spec"]["storageClassName"]=="longhorn" and p["status"]["phase"]=="Bound" for p in found.values()); volumes={v["metadata"]["name"]:v for v in json.loads(pathlib.Path(sys.argv[2]).read_text())["items"]}; replicas=json.loads(pathlib.Path(sys.argv[3]).read_text())["items"]; names={p["spec"]["volumeName"] for p in found.values()}; assert all(volumes[n]["status"]["robustness"]=="healthy" and volumes[n]["spec"]["dataEngine"]=="v1" for n in names); assert all({r["spec"]["nodeID"] for r in replicas if r["spec"]["volumeName"]==n}=={"worker-1","worker-2","worker-3"} for n in names)' "$temporary_directory/pvcs.json" "$temporary_directory/volumes.json" "$temporary_directory/replicas.json"

curl --fail --silent --show-error http://127.0.0.1:19090/api/v1/rules |
    uv run python -c 'import json,sys; d=json.load(sys.stdin); rules=[r for g in d["data"]["groups"] for r in g["rules"]]; wanted={"OpenTelemetryCollectorUnavailable","LokiUnavailable","TempoUnavailable","OpenTelemetryExporterFailures","TelemetryStorageCapacityCritical"}; found={r["name"] for r in rules}; assert wanted <= found; assert all(r.get("health")=="ok" for r in rules if r["name"] in wanted)'
test "$(kubectl_cmd get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')" = 6

trap - EXIT HUP INT TERM
cleanup
echo "Observability Part Two validation passed (${mode#--}): metrics, logs, traces, correlation, policy and persistent storage are healthy."
