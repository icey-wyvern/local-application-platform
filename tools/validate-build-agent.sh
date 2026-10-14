#!/bin/sh
set -eu

host=${1:?build-agent hostname is required}
address=${2:?build-agent address is required}
port=${3:?build-agent port is required}
ca=${4:?CA certificate is required}
client_certificate=${5:?client certificate is required}
client_key=${6:?client key is required}
repository=${7:-}
revision=${8:-main}
endpoint="https://${host}:${port}"
resolve="${host}:${port}:${address}"
temporary_directory=$(mktemp -d)
trap 'rm -rf -- "$temporary_directory"' EXIT HUP INT TERM

trusted_curl() {
    curl --noproxy '*' --connect-timeout 5 --max-time 30 \
        --silent --show-error --fail-with-body \
        --cacert "$ca" --cert "$client_certificate" --key "$client_key" \
        --resolve "$resolve" "$@"
}

trusted_curl "${endpoint}/healthz" | python3 -c \
    'import json,sys; assert json.load(sys.stdin) == {"status": "ok"}'

if curl --noproxy '*' --connect-timeout 5 --max-time 10 --silent --show-error \
    --cacert "$ca" --resolve "$resolve" \
    "${endpoint}/healthz" >/dev/null 2>&1; then
    echo 'build agent accepted a request without a client certificate' >&2
    exit 1
fi

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=untrusted-client' \
    -keyout "$temporary_directory/untrusted.key" \
    -out "$temporary_directory/untrusted.crt" >/dev/null 2>&1
if curl --noproxy '*' --connect-timeout 5 --max-time 10 --silent --show-error --cacert "$ca" \
    --cert "$temporary_directory/untrusted.crt" --key "$temporary_directory/untrusted.key" \
    --resolve "$resolve" "${endpoint}/healthz" >/dev/null 2>&1; then
    echo 'build agent accepted an untrusted client certificate' >&2
    exit 1
fi

invalid_url_response=$(trusted_curl -X POST -H 'content-type: application/json' \
    --data '{"buildId":"00000000-0000-4000-8000-000000000001","repositoryUrl":"git://example.com/repo.git","revision":{"type":"branch","value":"main"}}' \
    "${endpoint}/v1/builds" 2>/dev/null || true)
printf '%s' "$invalid_url_response" | python3 -c \
    'import json,sys; assert json.load(sys.stdin)["error"]["code"] == "repository_url_invalid"'

for forbidden_url in https://registry.lab.test/repo.git \
    https://10.20.0.12/repo.git https://127.0.0.1/repo.git \
    https://169.254.169.254/repo.git; do
    request=$(python3 -c 'import json,sys,uuid; print(json.dumps({"buildId":str(uuid.uuid4()),"repositoryUrl":sys.argv[1],"revision":{"type":"branch","value":"main"}}))' "$forbidden_url")
    private_url_response=$(trusted_curl -X POST -H 'content-type: application/json' \
        --data "$request" "${endpoint}/v1/builds" 2>/dev/null || true)
    printf '%s' "$private_url_response" | python3 -c \
        'import json,sys; assert json.load(sys.stdin)["error"]["code"] == "repository_destination_forbidden"'
done

if [ -n "$repository" ]; then
    redirect_url=$(python3 -c 'import sys,urllib.parse; value=urllib.parse.urlsplit(sys.argv[1]); print(urllib.parse.urlunsplit((value.scheme,value.netloc,"/redirect-private.git","","")))' "$repository")
    request=$(python3 -c 'import json,sys,uuid; print(json.dumps({"buildId":str(uuid.uuid4()),"repositoryUrl":sys.argv[1],"revision":{"type":"branch","value":"main"}}))' "$redirect_url")
    redirected_response=$(trusted_curl -X POST -H 'content-type: application/json' \
        --data "$request" "${endpoint}/v1/builds" 2>/dev/null || true)
    printf '%s' "$redirected_response" | python3 -c \
        'import json,sys; assert json.load(sys.stdin)["error"]["code"] == "repository_destination_forbidden"'
fi

if [ -n "$repository" ] && [ "${LAP_VALIDATE_REDIRECT_ONLY:-0}" != 1 ]; then
    build_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
    request=$(python3 -c 'import json,sys; print(json.dumps({"buildId":sys.argv[1],"repositoryUrl":sys.argv[2],"revision":{"type":"branch","value":sys.argv[3]}}))' "$build_id" "$repository" "$revision")
    result=$(trusted_curl -X POST -H 'content-type: application/json' --data "$request" \
        "${endpoint}/v1/builds")
    printf '%s' "$result" | python3 -c '
import json,re,sys
result=json.load(sys.stdin)
assert result["status"] == "succeeded"
assert re.fullmatch(r"[0-9a-f]{40}", result["commit"])
assert re.fullmatch(r"sha256:[0-9a-f]{64}", result["imageDigest"])
assert result["imageReference"].endswith("@" + result["imageDigest"])
print(json.dumps(result, sort_keys=True))'
fi

trusted_curl "${endpoint}/metrics" | grep -q 'lap_build_agent_builds_total'
echo 'Build-agent mTLS, URL security, and endpoint checks passed.'
