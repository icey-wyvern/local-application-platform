#!/usr/bin/env bash
set -euo pipefail

kubectl_bin=${1:-${HOME}/.local/bin/kubectl}
kubeconfig=${2:-${HOME}/.kube/lap-admin.conf}
port=18084
forward_pid=
restore() {
  "$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-database \
    scale statefulset/postgresql --replicas=1 >/dev/null 2>&1 || true
  if [[ -n "$forward_pid" ]]; then
    kill "$forward_pid" 2>/dev/null || true
    wait "$forward_pid" 2>/dev/null || true
  fi
}
trap restore EXIT HUP INT TERM

snapshot() {
  "$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-database \
    exec statefulset/postgresql -- psql -U postgres -d platform -At -c \
    "SELECT status, count(*) FROM operations GROUP BY status ORDER BY status" 2>/dev/null
}
before=$(snapshot)
"$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-system \
  port-forward service/platform-api "$port:8080" >/dev/null 2>&1 &
forward_pid=$!
for _ in $(seq 1 30); do
  curl -fsS --max-time 2 "http://127.0.0.1:$port/readyz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS --max-time 2 "http://127.0.0.1:$port/readyz" >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-database \
  scale statefulset/postgresql --replicas=0 >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-database \
  wait --for=delete pod/postgresql-0 --timeout=3m >/dev/null
unready=0
for _ in $(seq 1 45); do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 \
    "http://127.0.0.1:$port/readyz" 2>/dev/null || true)
  if [[ "$code" == 503 ]]; then unready=1; break; fi
  sleep 2
done
[[ "$unready" == 1 ]] || { echo 'API readiness did not report database outage' >&2; exit 1; }
[[ $(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 \
  "http://127.0.0.1:$port/healthz") == 200 ]]
"$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-database \
  scale statefulset/postgresql --replicas=1 >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-database \
  rollout status statefulset/postgresql --timeout=5m >/dev/null
ready=0
for _ in $(seq 1 60); do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 \
    "http://127.0.0.1:$port/readyz" 2>/dev/null || true)
  if [[ "$code" == 200 ]]; then ready=1; break; fi
  sleep 2
done
[[ "$ready" == 1 ]] || { echo 'API readiness did not recover' >&2; exit 1; }
after=$(snapshot)
[[ "$before" == "$after" ]] || { echo 'operation state changed during idle database outage' >&2; exit 1; }
"$kubectl_bin" --kubeconfig "$kubeconfig" -n platform-system \
  rollout status deployment/platform-api --timeout=3m >/dev/null
echo 'PostgreSQL outage, API readiness and durable operation recovery passed.'
