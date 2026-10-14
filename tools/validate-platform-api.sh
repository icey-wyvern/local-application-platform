#!/usr/bin/env bash
set -euo pipefail

kubectl_bin="${1:-${HOME}/.local/bin/kubectl}"
kubeconfig="${2:-${HOME}/.kube/lap-admin.conf}"
token_file="${3:-$HOME/.local/share/local-application-platform/secrets/platform-api-token}"
local_port="${4:-18080}"

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  port-forward service/platform-api "$local_port:8080" >/tmp/lap-platform-api-port-forward.log 2>&1 &
forward_pid=$!
trap 'kill "$forward_pid" 2>/dev/null || true; wait "$forward_pid" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  curl --silent --fail "http://127.0.0.1:$local_port/healthz" >/dev/null && break
  sleep 1
done

test "$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$local_port/healthz")" = 200
test "$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$local_port/readyz")" = 200
test "$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$local_port/api/v1/info")" = 401
token="$(<"$token_file")"
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  -H "Authorization: Bearer $token" "http://127.0.0.1:$local_port/api/v1/info")" = 200
curl --silent --fail "http://127.0.0.1:$local_port/metrics" \
  | grep lap_platform_api_http_requests_total >/dev/null
unset token

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  get deployment platform-api -o jsonpath='{.status.readyReplicas}' | grep -qx 2
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  exec deployment/platform-api -- python -c '
import asyncio
from lap_platform_api.app import create_dependencies
from lap_platform_api.config import ApiSettings

async def validate() -> None:
    dependencies = create_dependencies(ApiSettings.from_environment())
    try:
        assert await dependencies.build_agent.health()
    finally:
        await dependencies.close()

asyncio.run(validate())
'
printf 'platform API runtime validation passed\n'
