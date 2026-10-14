#!/bin/sh
set -eu

ssh_key=${1:?SSH key is required}
ssh_user=${2:?SSH user is required}
address=${3:?build host address is required}
host=${4:?build-agent hostname is required}
port=${5:?build-agent port is required}
ca=${6:?CA certificate is required}
client_certificate=${7:?client certificate is required}
client_key=${8:?client key is required}
endpoint="https://${host}:${port}/v1/builds"
resolve="${host}:${port}:${address}"

ssh -i "$ssh_key" -o BatchMode=yes "${ssh_user}@${address}" \
    'sudo -u lap-build-agent flock -x /var/lib/lap-build-agent/build.lock sleep 10' &
holder=$!
trap 'kill "$holder" 2>/dev/null || true; wait "$holder" 2>/dev/null || true' EXIT HUP INT TERM
sleep 1

request() {
    build_id=$1
    python3 -c 'import json,sys; print(json.dumps({"buildId":sys.argv[1],"repositoryUrl":"git://example.com/repo.git","revision":{"type":"branch","value":"main"}}))' "$build_id" |
        curl --noproxy '*' --connect-timeout 5 --max-time 15 --silent --show-error \
            --fail-with-body --cacert "$ca" --cert "$client_certificate" --key "$client_key" \
            --resolve "$resolve" -H 'content-type: application/json' --data-binary @- "$endpoint"
}

blocked=$(request "$(python3 -c 'import uuid; print(uuid.uuid4())')" 2>/dev/null || true)
printf '%s' "$blocked" | python3 -c \
    'import json,sys; assert json.load(sys.stdin)["error"]["code"] == "build_in_progress"'

wait "$holder"
trap - EXIT HUP INT TERM

recovered=$(request "$(python3 -c 'import uuid; print(uuid.uuid4())')" 2>/dev/null || true)
printf '%s' "$recovered" | python3 -c \
    'import json,sys; assert json.load(sys.stdin)["error"]["code"] == "repository_url_invalid"'
echo 'Build-agent flock concurrency and post-failure recovery checks passed.'
