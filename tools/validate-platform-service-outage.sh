#!/usr/bin/env bash
set -euo pipefail

mode=${1:?agent or registry is required}
repository=${2:?disposable public fixture URL is required}
kubectl_bin=${3:-${HOME}/.local/bin/kubectl}
kubeconfig=${4:-${HOME}/.kube/lap-admin.conf}
token_file=${5:-$HOME/.local/share/local-application-platform/secrets/platform-api-token}
case "$mode" in
  agent) service=lap-build-agent ;;
  registry) service=lap-registry ;;
  *) echo 'unknown service outage mode' >&2; exit 2 ;;
esac
port=18083
forward_pid=
restored=0
restore() {
  if [[ "$restored" == 0 ]]; then
    ssh -o BatchMode=yes -i ${HOME}/.ssh/lap_lab_ed25519 \
      ubuntu@10.20.0.51 sudo -n systemctl start "$service" >/dev/null 2>&1 || true
  fi
  if [[ "$mode" == registry ]]; then
    ssh -o BatchMode=yes -i ${HOME}/.ssh/lap_lab_ed25519 \
      ubuntu@10.20.0.51 sudo -n systemctl start lap-build-agent >/dev/null 2>&1 || true
  fi
  if [[ -n "$forward_pid" ]]; then
    kill "$forward_pid" 2>/dev/null || true
    wait "$forward_pid" 2>/dev/null || true
  fi
}
trap restore EXIT HUP INT TERM

"$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-system \
  port-forward service/platform-api "$port:8080" >/dev/null 2>&1 &
forward_pid=$!
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$port/readyz" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS --max-time 2 "http://127.0.0.1:$port/readyz" >/dev/null
token=$(tr -d '\n' <"$token_file")
ssh -o BatchMode=yes -i ${HOME}/.ssh/lap_lab_ed25519 \
  ubuntu@10.20.0.51 sudo -n systemctl stop "$service" >/dev/null
key=$(python3 -c 'import uuid; print(uuid.uuid4())')
payload=$(python3 -c 'import json,sys; print(json.dumps({"repositoryUrl":sys.argv[1],"revision":{"type":"branch","value":"success"}}))' "$repository")
response=$(curl -fsS --max-time 10 -H "Authorization: Bearer $token" \
  -H "Idempotency-Key: $key" -H 'Content-Type: application/json' \
  --data "$payload" "http://127.0.0.1:$port/api/v1/deployments")
operation_id=$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
build_id=$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["buildId"])')
if [[ "$mode" == registry ]]; then sleep 45; else sleep 8; fi
if [[ "$mode" == registry ]]; then
  [[ $(ssh -o BatchMode=yes -i ${HOME}/.ssh/lap_lab_ed25519 \
    ubuntu@10.20.0.51 systemctl is-active lap-build-agent) == active ]] || {
    echo 'registry outage stopped the build agent' >&2; exit 1;
  }
fi
response=$(curl -fsS --max-time 5 -H "Authorization: Bearer $token" \
  "http://127.0.0.1:$port/api/v1/operations/$operation_id")
printf '%s' "$response" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert value["status"] != "succeeded", value'
ssh -o BatchMode=yes -i ${HOME}/.ssh/lap_lab_ed25519 \
  ubuntu@10.20.0.51 sudo -n systemctl start "$service" >/dev/null
restored=1
for _ in $(seq 1 240); do
  response=$(curl -fsS --max-time 5 -H "Authorization: Bearer $token" \
    "http://127.0.0.1:$port/api/v1/operations/$operation_id")
  status=$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  [[ "$status" == succeeded || "$status" == failed ]] && break
  sleep 2
done
printf '%s' "$response" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert value["status"] == "succeeded" and value["attempts"] >= int(sys.argv[1]), value' \
  "$([[ "$mode" == agent ]] && echo 2 || echo 1)"
replayed=$(curl -fsS --max-time 10 -H "Authorization: Bearer $token" \
  -H "Idempotency-Key: $key" -H 'Content-Type: application/json' \
  --data "$payload" "http://127.0.0.1:$port/api/v1/deployments")
printf '%s' "$replayed" | python3 -c 'import json,sys; assert json.load(sys.stdin)["id"] == sys.argv[1]' "$operation_id"
row=$("$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-database \
  exec statefulset/postgresql -- psql -U postgres -d platform -At -c \
  "SELECT (SELECT count(*) FROM builds WHERE id='$build_id'), (SELECT count(*) FROM releases WHERE build_id='$build_id'), (SELECT count(*) FROM deployments WHERE operation_id='$operation_id')" 2>/dev/null)
[[ "$row" == '1|1|1' ]] || { echo "unexpected Build/Release/Deployment counts: $row" >&2; exit 1; }
echo "Service outage recovery, idempotency and single publication passed ($mode)."
