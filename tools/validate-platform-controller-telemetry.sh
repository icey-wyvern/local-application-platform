#!/bin/sh
set -eu

kubectl=${1:?kubectl path is required}
kubeconfig=${2:?kubeconfig is required}
namespace=${3:-monitoring}
temporary_directory=$(mktemp -d)
forward_pid=

cleanup() {
    if [ -n "$forward_pid" ]; then
        kill "$forward_pid" 2>/dev/null || true
        wait "$forward_pid" 2>/dev/null || true
    fi
    rm -rf -- "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

start_forward() {
    local_port=$1
    remote=$2
    remote_port=$3
    readiness_path=${4:-/ready}
    "$kubectl" --kubeconfig "$kubeconfig" --namespace "$namespace" \
        port-forward "$remote" "${local_port}:${remote_port}" \
        >"$temporary_directory/port-forward.log" 2>&1 &
    forward_pid=$!
    attempts=0
    while ! curl --silent --fail --max-time 2 \
        "http://127.0.0.1:${local_port}${readiness_path}" >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 20 ]; then
            cat "$temporary_directory/port-forward.log" >&2
            return 1
        fi
        sleep 1
    done
}

stop_forward() {
    kill "$forward_pid" 2>/dev/null || true
    wait "$forward_pid" 2>/dev/null || true
    forward_pid=
}

wait_for_query() {
    url=$1
    query=$2
    attempts=0
    until curl --silent --show-error --fail --get \
        --data-urlencode "$query" "$url" \
        | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["data"]["result"]'; do
        attempts=$((attempts + 1))
        [ "$attempts" -lt 30 ] || return 1
        sleep 3
    done
}

start_forward 13101 service/loki 3100
wait_for_query 'http://127.0.0.1:13101/loki/api/v1/query_range' \
    'query={service_name="platform-controller"}'
stop_forward

start_forward 13201 service/tempo 3200
attempts=0
until curl --silent --show-error --fail --get \
    --data-urlencode 'tags=service.name=platform-controller' \
    --data-urlencode 'limit=20' 'http://127.0.0.1:13201/api/search' \
    | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["traces"]'; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 30 ] || exit 1
    sleep 3
done
stop_forward

start_forward 19091 service/kube-prometheus-stack-prometheus 9090 /-/ready
wait_for_query 'http://127.0.0.1:19091/api/v1/query' \
    'query=lap_platform_controller_reconciliations_total'
wait_for_query 'http://127.0.0.1:19091/api/v1/query' \
    'query=lap_platform_controller_health_checks_total'
stop_forward

echo 'Platform controller logs, traces, and metrics reached Loki, Tempo, and Prometheus.'
