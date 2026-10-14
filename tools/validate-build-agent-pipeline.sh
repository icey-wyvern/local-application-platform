#!/bin/sh
set -eu

host=${1:?build-agent hostname is required}
address=${2:?build-agent address is required}
port=${3:?build-agent port is required}
ca=${4:?CA certificate is required}
client_certificate=${5:?client certificate is required}
client_key=${6:?client key is required}
repository=${7:?public fixture repository is required}
ssh_key=${8:?SSH key is required}
endpoint="https://${host}:${port}/v1/builds"
resolve="${host}:${port}:${address}"

post_build() {
    build_id=$1
    branch=$2
    payload=$(python3 -c 'import json,sys; print(json.dumps({"buildId":sys.argv[1],"repositoryUrl":sys.argv[2],"revision":{"type":"branch","value":sys.argv[3]}}))' "$build_id" "$repository" "$branch")
    curl --noproxy '*' --connect-timeout 5 --max-time 900 --silent --show-error \
        --cacert "$ca" --cert "$client_certificate" --key "$client_key" \
        --resolve "$resolve" -H 'content-type: application/json' --data "$payload" "$endpoint"
}

assert_error() {
    response=$1
    expected=$2
    printf '%s' "$response" | python3 -c \
        'import json,sys; data=json.load(sys.stdin); assert data["error"]["code"] == sys.argv[1], data' \
        "$expected"
}

assert_not_published() {
    application=$1
    build_id=$2
    if ssh -i "$ssh_key" -o BatchMode=yes "ubuntu@${address}" \
        "sudo -u lap-build-agent skopeo inspect --authfile /var/lib/lap-build-agent/registry-auth.json docker://registry.lab.test:5000/apps/${application}:build-${build_id}" \
        >/dev/null 2>&1; then
        echo "rejected artifact ${application}:build-${build_id} was published" >&2
        exit 1
    fi
}

invalid_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
invalid_response=$(post_build "$invalid_id" invalid-config)
assert_error "$invalid_response" platform_config_invalid

oversized_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
oversized_response=$(post_build "$oversized_id" oversized-config)
assert_error "$oversized_response" platform_config_too_large

failure_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
failure_response=$(post_build "$failure_id" failed-build)
assert_error "$failure_response" build_failed
assert_not_published build-agent-failure "$failure_id"

vulnerable_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
vulnerable_response=$(post_build "$vulnerable_id" vulnerable)
assert_error "$vulnerable_response" vulnerability_policy_rejected
assert_not_published build-agent-vulnerable "$vulnerable_id"
printf '%s' "$vulnerable_response" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["error"]["message"])'

echo 'Invalid and oversized configuration, failed build, and vulnerability no-publication checks passed.'
