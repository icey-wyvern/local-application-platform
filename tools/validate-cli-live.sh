#!/usr/bin/env bash
set -euo pipefail

repository=${1:?public fixture repository URL is required}
application=${2:?disposable application name is required}
api_ip=${3:-10.20.0.100}
admin_user=${LAP_CLI_USER:-$(stat -c %U .)}
admin_home=$(getent passwd "$admin_user" | cut -d: -f6)
[ -n "$admin_home" ] || { echo 'CLI user has no home directory' >&2; exit 1; }
token_file=${LAP_CLI_TOKEN_FILE:-$admin_home/.local/share/local-application-platform/secrets/platform-api-token}
ca_bundle=${LAP_CLI_CA_BUNDLE:-.generated/pki/lab-root-ca.crt}

diagnostics=$(mktemp -d)
step=setup
added_host=0
cleanup() {
  result=$?
  if [ "$added_host" -eq 1 ]; then
    sed -i "/^$api_ip api.lab.test$/d" /etc/hosts || true
  fi
  if [ "$result" -ne 0 ]; then
    printf 'FAIL CLI live acceptance at %s\n' "$step" >&2
    for output in "$diagnostics"/*; do
      [ -f "$output" ] && { printf '%s\n' "$(basename "$output")" >&2; cat "$output" >&2; }
    done
  fi
  rm -rf -- "$diagnostics"
  exit "$result"
}
trap cleanup EXIT

if ! getent hosts api.lab.test >/dev/null; then
  [ "$(id -u)" -eq 0 ] || { echo 'api.lab.test must resolve before validation' >&2; exit 1; }
  printf '%s api.lab.test\n' "$api_ip" >>/etc/hosts
  added_host=1
fi
export LAP_API_TOKEN
LAP_API_TOKEN=$(tr -d '\n' <"$token_file")
export LAP_CA_BUNDLE="$ca_bundle" NO_PROXY=api.lab.test no_proxy=api.lab.test
if [ "$(id -un)" = "$admin_user" ]; then
  cli=("$admin_home/.local/bin/uv" run --package lap-cli lap)
else
  [ "$(id -u)" -eq 0 ] || { echo 'CLI validation requires the repository owner or root' >&2; exit 1; }
  cli=(runuser -u "$admin_user" -- "$admin_home/.local/bin/uv" run --package lap-cli lap)
fi

step=deploy
"${cli[@]}" deploy "$repository" --revision success >"$diagnostics/deploy" 2>&1
grep -q 'Application:' "$diagnostics/deploy"
grep -q "https://$application.apps.lab.test" "$diagnostics/deploy"
printf 'PASS CLI deploy and Operation polling\n'

step=status
"${cli[@]}" --json status "$application" >"$diagnostics/status" 2>&1
python3 - "$application" "$diagnostics/status" <<'PY'
import json, sys
name, path = sys.argv[1:]
value = json.load(open(path, encoding="utf-8"))
assert value["application"]["name"] == name
assert value["release"] is not None
assert value["latestDeployment"]["status"] == "healthy"
assert value["latestOperation"]["status"] == "succeeded"
PY
printf 'PASS CLI status persisted result\n'

step=history
"${cli[@]}" --json history "$application" >"$diagnostics/history" 2>&1
python3 - "$diagnostics/history" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["builds"] and value["releases"] and value["deployments"] and value["operations"]
assert any(item["status"] == "succeeded" for item in value["operations"])
PY
printf 'PASS CLI history persisted result\n'

step=https
curl --silent --show-error --fail --noproxy '*' --cacert "$ca_bundle" \
  --resolve "$application.apps.lab.test:443:$api_ip" \
  "https://$application.apps.lab.test/health" >"$diagnostics/https" 2>&1
grep -qx ok "$diagnostics/https"
printf 'PASS application HTTPS\n'

step=delete
"${cli[@]}" delete "$application" >"$diagnostics/delete" 2>&1
grep -q 'Delete: succeeded' "$diagnostics/delete"
"${cli[@]}" --json list >"$diagnostics/list" 2>&1
python3 - "$application" "$diagnostics/list" <<'PY'
import json, sys
name, path = sys.argv[1:]
assert all(item["name"] != name for item in json.load(open(path, encoding="utf-8")))
PY
printf 'PASS CLI delete and application removal\n'
