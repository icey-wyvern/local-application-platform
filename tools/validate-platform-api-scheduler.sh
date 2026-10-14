#!/usr/bin/env bash
set -euo pipefail

kubectl_bin="${1:-${HOME}/.local/bin/kubectl}"
kubeconfig="${2:-${HOME}/.kube/lap-admin.conf}"
token_file="${3:-$HOME/.local/share/local-application-platform/secrets/platform-api-token}"
local_port="${4:-18081}"
temporary_directory="$(mktemp -d)"
forward_pid=

cleanup() {
  if [[ -n "$forward_pid" ]]; then
    kill "$forward_pid" 2>/dev/null || true
    wait "$forward_pid" 2>/dev/null || true
  fi
  rm -rf -- "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

start_forward() {
  local namespace=$1
  local resource=$2
  local ports=$3
  if [[ -n "$forward_pid" ]]; then
    kill "$forward_pid" 2>/dev/null || true
    wait "$forward_pid" 2>/dev/null || true
  fi
  "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" \
    port-forward "$resource" "$ports" >"$temporary_directory/port-forward.log" 2>&1 &
  forward_pid=$!
}

wait_for_http() {
  local url=$1
  for _ in $(seq 1 30); do
    curl --silent --fail --max-time 2 "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  cat "$temporary_directory/port-forward.log" >&2
  return 1
}

database_query() {
  local query=$1
  "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-database \
    exec statefulset/postgresql -- psql -U postgres -d platform -At -F '|' -c "$query" \
    2>/dev/null
}

submit_operation() {
  local suffix=$1
  local key
  key="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  curl --silent --show-error --fail \
    -H "Authorization: Bearer $token" \
    -H "Idempotency-Key: $key" \
    -H 'Content-Type: application/json' \
    --data "{\"repositoryUrl\":\"https://example.com/scheduler-validation-${suffix}.git\",\"revision\":{\"type\":\"branch\",\"value\":\"main\"}}" \
    "http://127.0.0.1:${local_port}/api/v1/deployments" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'
}

wait_for_sql() {
  local query=$1
  local expected=$2
  local attempts=${3:-90}
  local result
  for _ in $(seq 1 "$attempts"); do
    result="$(database_query "$query")"
    [[ "$result" == "$expected" ]] && return 0
    sleep 1
  done
  printf 'last SQL result: %s\n' "$result" >&2
  return 1
}

start_forward platform-system service/platform-api "${local_port}:8080"
wait_for_http "http://127.0.0.1:${local_port}/readyz"
token="$(<"$token_file")"
run_id="$(date +%s)-$$"
ids=()
for index in 1 2 3 4; do
  ids+=("$(submit_operation "${run_id}-${index}")")
done
id_list="'${ids[0]}','${ids[1]}','${ids[2]}','${ids[3]}'"

wait_for_sql \
  "SELECT count(*) FROM operations WHERE id IN (${id_list}) AND status = 'running'" \
  4 45
owner_counts="$(database_query \
  "SELECT lease_owner, count(*) FROM operations WHERE id IN (${id_list}) AND status = 'running' GROUP BY lease_owner ORDER BY lease_owner")"
[[ "$(printf '%s\n' "$owner_counts" | wc -l)" -eq 2 ]]
[[ "$(printf '%s\n' "$owner_counts" | awk -F '|' '$2 == 2 {count++} END {print count+0}')" -eq 2 ]]

first_expiry="$(database_query \
  "SELECT extract(epoch FROM lease_expires_at) FROM operations WHERE id = '${ids[0]}'")"
sleep 11
renewed_expiry="$(database_query \
  "SELECT extract(epoch FROM lease_expires_at) FROM operations WHERE id = '${ids[0]}'")"
python3 - "$first_expiry" "$renewed_expiry" <<'PY'
import sys

assert float(sys.argv[2]) > float(sys.argv[1])
PY
wait_for_sql \
  "SELECT count(*) FROM operations WHERE id IN (${id_list}) AND status = 'succeeded'" \
  4 45

recovery_id="$(submit_operation "${run_id}-recovery")"
wait_for_sql \
  "SELECT count(*) FROM operations WHERE id = '${recovery_id}' AND status = 'running' AND attempts = 1" \
  1 30
old_owner="$(database_query "SELECT lease_owner FROM operations WHERE id = '${recovery_id}'")"
old_pod="${old_owner%%:*}"
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  delete pod "$old_pod" --grace-period=0 --force --wait=false >/dev/null

reclaimed_owner=
for _ in $(seq 1 100); do
  row="$(database_query \
    "SELECT status, attempts, coalesce(lease_owner, '') FROM operations WHERE id = '${recovery_id}'")"
  IFS='|' read -r status attempts owner <<<"$row"
  if [[ "$status" == running && "$attempts" -ge 2 && "$owner" != "$old_owner" ]]; then
    reclaimed_owner=$owner
    break
  fi
  sleep 1
done
[[ -n "$reclaimed_owner" ]]
wait_for_sql \
  "SELECT count(*) FROM operations WHERE id = '${recovery_id}' AND status = 'succeeded' AND attempts >= 2" \
  1 45
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  rollout status deployment/platform-api --timeout=3m >/dev/null
[[ "$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  get deployment platform-api -o jsonpath='{.status.readyReplicas}')" == 2 ]]

start_forward platform-system service/platform-api "${local_port}:8080"
wait_for_http "http://127.0.0.1:${local_port}/readyz"
curl --silent --fail "http://127.0.0.1:${local_port}/metrics" \
  | grep -q lap_platform_api_scheduler_claims_total
unset token

start_forward monitoring service/loki '13101:3100'
wait_for_http 'http://127.0.0.1:13101/ready'
found=0
for _ in $(seq 1 30); do
  if curl --silent --fail --get \
    --data-urlencode 'query={service_name="platform-api"} |= "operation execution finished"' \
    'http://127.0.0.1:13101/loki/api/v1/query_range' \
    | python3 -c 'import json,sys; assert json.load(sys.stdin)["data"]["result"]' 2>/dev/null; then
    found=1
    break
  fi
  sleep 3
done
[[ "$found" -eq 1 ]]

start_forward monitoring service/tempo '13201:3200'
wait_for_http 'http://127.0.0.1:13201/ready'
found=0
for _ in $(seq 1 30); do
  if curl --silent --fail --get \
    --data-urlencode 'q={ resource.service.name = "platform-api" && name = "operation.execute" }' \
    'http://127.0.0.1:13201/api/search' \
    | python3 -c 'import json,sys; assert json.load(sys.stdin)["traces"]' 2>/dev/null; then
    found=1
    break
  fi
  sleep 3
done
[[ "$found" -eq 1 ]]

start_forward monitoring service/kube-prometheus-stack-prometheus '19091:9090'
wait_for_http 'http://127.0.0.1:19091/-/ready'
found=0
for _ in $(seq 1 30); do
  if curl --silent --fail --get \
    --data-urlencode 'query=lap_platform_api_scheduler_claims_total' \
    'http://127.0.0.1:19091/api/v1/query' \
    | python3 -c 'import json,sys; assert json.load(sys.stdin)["data"]["result"]' 2>/dev/null; then
    found=1
    break
  fi
  sleep 3
done
[[ "$found" -eq 1 ]]

printf 'Two-replica scheduler concurrency, renewal, Pod-loss recovery, and telemetry passed.\n'
