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
    while ! curl --silent --fail --max-time 2 "http://127.0.0.1:${local_port}${readiness_path}" >/dev/null 2>&1; do
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

start_forward 13100 service/loki 3100
curl --silent --show-error --fail --get \
    --data-urlencode 'query={service_name="platform-build-agent"}' \
    --data-urlencode 'limit=20' \
    'http://127.0.0.1:13100/loki/api/v1/query_range' |
    python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["data"]["result"]'
stop_forward

start_forward 13200 service/tempo 3200
curl --silent --show-error --fail --get \
    --data-urlencode 'tags=service.name=platform-build-agent' \
    --data-urlencode 'limit=20' \
    'http://127.0.0.1:13200/api/search' |
    python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["traces"]'
stop_forward

start_forward 19090 service/kube-prometheus-stack-prometheus 9090 /-/ready
curl --silent --show-error --fail --get \
    --data-urlencode 'query=lap_build_agent_stages_total' \
    'http://127.0.0.1:19090/api/v1/query' |
    python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["data"]["result"]'
stop_forward

echo 'Build-agent logs, traces, and metrics reached Loki, Tempo, and Prometheus.'
