#!/bin/sh
set -eu

if [ "$#" -lt 10 ] || [ "$#" -gt 11 ]; then
    echo "usage: $0 HELM KUBECTL KUBECONFIG VERSION ROOT_CA GATEWAY_IP API_VIP HOST ADMIN_PASSWORD VALIDATION_MANIFESTS [--failure]" >&2
    exit 2
fi

helm_binary=$1
kubectl_binary=$2
kubeconfig=$3
version=$4
root_ca=$5
gateway_ip=$6
api_vip=$7
hostname=$8
admin_password=$9
validation_manifests=${10}
mode=${11:-normal}
namespace=monitoring
temporary_directory=$(mktemp -d)
forward_pids=
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
windows_validation=$(wslpath -w "$repo_root/tools/validate-windows-grafana-tls.ps1")

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

cleanup() {
    kubectl_cmd delete --kustomize "$validation_manifests" --ignore-not-found >/dev/null 2>&1 || true
    for pid in $forward_pids; do
        kill "$pid" >/dev/null 2>&1 || true
        wait "$pid" >/dev/null 2>&1 || true
    done
    rm -rf "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

start_forward() {
    service=$1
    mapping=$2
    "$kubectl_binary" --kubeconfig "$kubeconfig" --namespace "$namespace" \
        port-forward "service/$service" "$mapping" \
        >"$temporary_directory/port-forward-${service}.log" 2>&1 &
    pid=$!
    forward_pids="$forward_pids $pid"
}

wait_http() {
    url=$1
    for attempt in $(seq 1 60); do
        if curl --fail --silent --show-error --output /dev/null "$url" 2>/dev/null; then
            return
        fi
        sleep 1
    done
    echo "endpoint did not become healthy: $url" >&2
    return 1
}

release=$("$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$namespace" \
    --filter '^kube-prometheus-stack$' --output json)
printf '%s' "$release" | grep -q "\"chart\":\"kube-prometheus-stack-${version}\""

kubectl_cmd --namespace "$namespace" rollout status deployment/kube-prometheus-stack-operator --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status deployment/kube-prometheus-stack-kube-state-metrics --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status statefulset/kube-prometheus-stack-grafana --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status statefulset/prometheus-kube-prometheus-stack-prometheus --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status statefulset/alertmanager-kube-prometheus-stack-alertmanager --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status daemonset/kube-prometheus-stack-prometheus-node-exporter --timeout=5m
test "$(kubectl_cmd --namespace "$namespace" get daemonset kube-prometheus-stack-prometheus-node-exporter -o jsonpath='{.status.numberReady}')" = 6

for service in kube-prometheus-stack-prometheus kube-prometheus-stack-alertmanager kube-prometheus-stack-grafana; do
    test "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.spec.type}')" = ClusterIP
    test -z "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.spec.externalIPs}')"
done

test "$(kubectl_cmd --namespace "$namespace" get prometheus kube-prometheus-stack-prometheus -o jsonpath='{.spec.retention}')" = 7d
test "$(kubectl_cmd --namespace "$namespace" get prometheus kube-prometheus-stack-prometheus -o jsonpath='{.spec.retentionSize}')" = 8GB

kubectl_cmd --namespace envoy-gateway-system wait --for=condition=Ready certificate/grafana --timeout=3m
test "$(kubectl_cmd --namespace envoy-gateway-system get certificate grafana -o jsonpath='{.spec.dnsNames[0]}')" = "$hostname"
for route in grafana grafana-http-to-https; do
    route_ready=false
    for attempt in $(seq 1 60); do
        if kubectl_cmd --namespace "$namespace" get httproute "$route" \
            -o jsonpath='{range .status.parents[*].conditions[*]}{.type}={.status}{"\n"}{end}' |
            grep -qx 'Accepted=True'; then
            route_ready=true
            break
        fi
        sleep 1
    done
    test "$route_ready" = true
done

start_forward kube-prometheus-stack-prometheus 19090:9090
start_forward kube-prometheus-stack-alertmanager 19093:9093
start_forward kube-prometheus-stack-grafana 13000:80
wait_http http://127.0.0.1:19090/-/ready
wait_http http://127.0.0.1:19093/-/ready
wait_http http://127.0.0.1:13000/api/health

curl --fail --silent --show-error http://127.0.0.1:19090/api/v1/targets \
    >"$temporary_directory/targets.json"
uv run python -c \
    'import json,pathlib; d=json.loads(pathlib.Path("'$temporary_directory'/targets.json").read_text())["data"]["activeTargets"]; required=("apiserver","kubelet","kube-state-metrics","node-exporter","longhorn","envoy-gateway"); healthy=[t for t in d if t["health"]=="up"]; assert all(any(name in t["labels"].get("job","") for t in healthy) for name in required), [(t["labels"].get("job"),t["health"],t.get("lastError")) for t in d]; assert not [t for t in d if t["health"]=="down"], [(t["labels"].get("job"),t.get("lastError")) for t in d if t["health"]=="down"]'

for query in \
    'count(kube_node_info) == 6' \
    'count(node_uname_info) >= 6' \
    'count(kube_pod_info) > 0' \
    'sum(up{job="apiserver"}) == 3' \
    'count(longhorn_volume_robustness) > 0' \
    'count(controller_runtime_reconcile_total{job="envoy-gateway"}) > 0'; do
    curl --fail --silent --show-error --get \
        --data-urlencode "query=$query" http://127.0.0.1:19090/api/v1/query |
        uv run python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["data"]["result"], d'
done

printf 'machine 127.0.0.1 login admin password %s\n' "$(cat "$admin_password")" \
    >"$temporary_directory/grafana.netrc"
chmod 600 "$temporary_directory/grafana.netrc"
curl --fail --silent --show-error --netrc-file "$temporary_directory/grafana.netrc" \
    http://127.0.0.1:13000/api/datasources/uid/prometheus/health |
    uv run python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="OK", d'
for dashboard in lap-cluster-health lap-platform-health; do
    curl --fail --silent --show-error --netrc-file "$temporary_directory/grafana.netrc" \
        "http://127.0.0.1:13000/api/dashboards/uid/$dashboard" |
        uv run python -c 'import json,sys; d=json.load(sys.stdin); assert d["dashboard"]["uid"]==sys.argv[1]' "$dashboard"
done

python3 infrastructure/ansible/playbooks/files/query_dns.py "$api_vip" "$hostname" "$gateway_ip"
curl --fail --silent --show-error --output /dev/null \
    --resolve "${hostname}:443:${gateway_ip}" "https://${hostname}/api/health"
redirect_headers=$(curl --silent --show-error --head \
    --resolve "${hostname}:80:${gateway_ip}" "http://${hostname}/")
printf '%s\n' "$redirect_headers" | grep -Eq '^HTTP/[0-9.]+ 308'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$windows_validation" \
    -Address "$gateway_ip" -DnsServer "$api_vip" -Hostname "$hostname" >/dev/null

kubectl_cmd delete --kustomize "$validation_manifests" --ignore-not-found >/dev/null
kubectl_cmd apply --server-side --field-manager=lap-validation \
    --kustomize "$validation_manifests" >/dev/null
alert_firing=false
for attempt in $(seq 1 90); do
    if curl --fail --silent --show-error --get \
        --data-urlencode 'query=ALERTS{alertname="LAPObservabilityValidation",alertstate="firing"}' \
        http://127.0.0.1:19090/api/v1/query |
        uv run python -c 'import json,sys; assert json.load(sys.stdin)["data"]["result"]' 2>/dev/null; then
        alert_firing=true
        break
    fi
    sleep 1
done
test "$alert_firing" = true
alert_delivered=false
for attempt in $(seq 1 60); do
    if curl --fail --silent --show-error http://127.0.0.1:19093/api/v2/alerts |
        uv run python -c 'import json,sys; assert any(a["labels"].get("alertname")=="LAPObservabilityValidation" for a in json.load(sys.stdin))' 2>/dev/null; then
        alert_delivered=true
        break
    fi
    sleep 1
done
test "$alert_delivered" = true
kubectl_cmd delete --kustomize "$validation_manifests" --ignore-not-found >/dev/null

if [ "$mode" = --failure ]; then
    old_pod=$(kubectl_cmd --namespace "$namespace" get pod -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.name}')
    old_uid=$(kubectl_cmd --namespace "$namespace" get pod "$old_pod" -o jsonpath='{.metadata.uid}')
    pvc_uid=$(kubectl_cmd --namespace "$namespace" get pvc -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.uid}')
    credential_checksum=$(cksum "$admin_password")
    kubectl_cmd --namespace "$namespace" delete pod "$old_pod" --wait=false >/dev/null
    kubectl_cmd --namespace "$namespace" wait --for=delete "pod/$old_pod" --timeout=3m
    kubectl_cmd --namespace "$namespace" rollout status statefulset/kube-prometheus-stack-grafana --timeout=10m
    test -z "$(kubectl_cmd --namespace "$namespace" get pods -l app.kubernetes.io/name=grafana -o jsonpath='{range .items[*]}{.metadata.uid}{"\n"}{end}' | grep -x "$old_uid" || true)"
    test "$pvc_uid" = "$(kubectl_cmd --namespace "$namespace" get pvc -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.uid}')"
    test "$credential_checksum" = "$(cksum "$admin_password")"
    start_forward kube-prometheus-stack-grafana 13001:80
    wait_http http://127.0.0.1:13001/api/health
    for attempt in $(seq 1 60); do
        if curl --fail --silent --show-error --netrc-file "$temporary_directory/grafana.netrc" \
            http://127.0.0.1:13001/api/dashboards/uid/lap-cluster-health >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done
    test "$attempt" -lt 60
    curl --fail --silent --show-error --output /dev/null \
        --resolve "${hostname}:443:${gateway_ip}" "https://${hostname}/api/health"
fi

kubectl_cmd --namespace "$namespace" get pvc -o json >"$temporary_directory/pvcs.json"
kubectl_cmd --namespace longhorn-system get volumes.longhorn.io -o json >"$temporary_directory/volumes.json"
kubectl_cmd --namespace longhorn-system get replicas.longhorn.io -o json >"$temporary_directory/replicas.json"
uv run python -c \
    'import json,pathlib,sys; pvcs=json.loads(pathlib.Path(sys.argv[1]).read_text())["items"]; volumes={v["metadata"]["name"]:v for v in json.loads(pathlib.Path(sys.argv[2]).read_text())["items"]}; replicas=json.loads(pathlib.Path(sys.argv[3]).read_text())["items"]; expected={"prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0":"10Gi","storage-kube-prometheus-stack-grafana-0":"2Gi","alertmanager-kube-prometheus-stack-alertmanager-db-alertmanager-kube-prometheus-stack-alertmanager-0":"2Gi"}; selected={p["metadata"]["name"]:p for p in pvcs if p["metadata"]["name"] in expected}; assert {n:p["spec"]["resources"]["requests"]["storage"] for n,p in selected.items()}==expected; assert all(p["spec"]["storageClassName"]=="longhorn" and p["status"]["phase"]=="Bound" for p in selected.values()); names={p["spec"]["volumeName"] for p in selected.values()}; assert all(volumes[n]["status"]["robustness"]=="healthy" and volumes[n]["spec"]["dataEngine"]=="v1" for n in names); assert all({r["spec"]["nodeID"] for r in replicas if r["spec"]["volumeName"]==n}=={"worker-1","worker-2","worker-3"} for n in names)' \
    "$temporary_directory/pvcs.json" "$temporary_directory/volumes.json" "$temporary_directory/replicas.json"

test "$(kubectl_cmd get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')" = 6
kubectl_cmd --namespace platform-database rollout status statefulset/postgresql --timeout=3m
test -z "$(kubectl_cmd --namespace "$namespace" get prometheusrule lap-observability-validation --ignore-not-found -o name)"

trap - EXIT HUP INT TERM
cleanup
echo "Observability validation passed (${mode#--}): metrics, dashboards, alert delivery, TLS and storage are healthy."
