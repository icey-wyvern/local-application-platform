#!/usr/bin/env bash
set -euo pipefail

kubectl_bin=${1:-${HOME}/.local/bin/kubectl}
kubeconfig=${2:-${HOME}/.kube/lap-admin.conf}
token_file=${3:-$HOME/.local/share/local-application-platform/secrets/platform-api-token}
api=https://api.lab.test/api/v1
k() { "$kubectl_bin" --kubeconfig "$kubeconfig" "$@"; }
assert_can_i() {
  local expected=$1 subject=$2 verb=$3 resource=$4 namespace=$5
  [[ $(k auth can-i "$verb" "$resource" -n "$namespace" --as "$subject") == "$expected" ]] || {
    echo "RBAC mismatch: $subject $verb $resource in $namespace" >&2; exit 1;
  }
}
api_sa=system:serviceaccount:platform-system:platform-api
controller_sa=system:serviceaccount:platform-system:platform-controller
assert_can_i yes "$api_sa" create applications.platform.lab.test platform-system
[[ $(k auth can-i patch applications.platform.lab.test --subresource=status --as "$api_sa") == no ]] || {
  echo 'API can patch Application status' >&2; exit 1;
}
assert_can_i no "$api_sa" create deployments.apps platform-system
assert_can_i no "$api_sa" get secrets platform-system
assert_can_i no "$api_sa" create clusterroles.rbac.authorization.k8s.io kube-system
assert_can_i yes "$controller_sa" create deployments.apps platform-system
assert_can_i no "$controller_sa" create pods platform-system
assert_can_i no "$controller_sa" list secrets platform-system
assert_can_i no "$controller_sa" create clusterroles.rbac.authorization.k8s.io kube-system
runtime_ns=$(k get namespace -l platform.lab.test/application -o json \
  | python3 -c 'import json,sys; names=sorted(item["metadata"]["name"] for item in json.load(sys.stdin)["items"]); assert names; print(names[0])')
app_sa="system:serviceaccount:$runtime_ns:application"
for resource in pods deployments.apps secrets applications.platform.lab.test; do
  assert_can_i no "$app_sa" get "$resource" "$runtime_ns"
done
k -n "$runtime_ns" get serviceaccount application -o json \
  | python3 -c 'import json,sys; assert json.load(sys.stdin)["automountServiceAccountToken"] is False'
k -n "$runtime_ns" get networkpolicy default-deny -o json \
  | python3 -c 'import json,sys; policy=json.load(sys.stdin)["spec"]; assert policy["podSelector"]=={} and set(policy["policyTypes"])=={"Ingress","Egress"}'
other_ns=$(k get namespace -l platform.lab.test/application -o json \
  | python3 -c 'import json,sys; names=sorted(item["metadata"]["name"] for item in json.load(sys.stdin)["items"]); assert len(names)>1; print(next(name for name in names if name!=sys.argv[1]))' "$runtime_ns")
workload_pod=$(k -n "$runtime_ns" get pod -l app.kubernetes.io/managed-by=platform-controller -o json \
  | python3 -c 'import json,sys; pods=json.load(sys.stdin)["items"]; assert pods; print(pods[0]["metadata"]["name"])')
for destination in "application.$other_ns.svc.cluster.local:8080" \
  '10.20.0.12:6443' '10.96.0.1:443' '169.254.169.254:443'; do
  host=${destination%:*}
  port=${destination##*:}
  if k -n "$runtime_ns" exec "$workload_pod" -- nc -z -w 3 "$host" "$port" >/dev/null 2>&1; then
    echo "application egress reached forbidden destination $destination" >&2; exit 1
  fi
done

for authorization in missing invalid; do
  if [[ "$authorization" == missing ]]; then
    code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time 5 "$api/info")
  else
    code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time 5 \
      -H 'Authorization: Bearer invalid' "$api/info")
  fi
  [[ "$code" == 401 ]] || { echo "API accepted $authorization bearer token" >&2; exit 1; }
done
token=$(tr -d '\n' <"$token_file")
for url in 'http://example.com/repo.git' 'https://user:password@example.com/repo.git' \
  'https://example.com:8443/repo.git'; do
  payload=$(python3 -c 'import json,sys; print(json.dumps({"repositoryUrl":sys.argv[1],"revision":{"type":"branch","value":"main"}}))' "$url")
  code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time 5 \
    -H "Authorization: Bearer $token" -H 'Idempotency-Key: 00000000-0000-4000-8000-000000000001' \
    -H 'Content-Type: application/json' --data "$payload" "$api/deployments")
  [[ "$code" == 422 ]] || { echo "unsafe API repository URL was accepted: $code" >&2; exit 1; }
done
code=$(python3 -c 'print("x" * 100000)' | curl --noproxy '*' -sS -o /dev/null \
  -w '%{http_code}' --max-time 5 -H "Authorization: Bearer $token" \
  -H 'Idempotency-Key: 00000000-0000-4000-8000-000000000001' \
  -H 'Content-Type: application/json' --data-binary @- "$api/deployments")
[[ "$code" == 413 || "$code" == 422 ]] || { echo "oversized API input returned $code" >&2; exit 1; }
unset token

[[ $(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time 5 \
  --cacert .generated/pki/lab-root-ca.crt https://registry.lab.test:5000/v2/) == 401 ]] || {
  echo 'registry accepted anonymous access' >&2; exit 1;
}
temporary_directory=$(mktemp -d)
probe_name="lap-security-probe-$(date +%s)-$$"
trap 'k -n default delete secret "$probe_name" --ignore-not-found >/dev/null 2>&1 || true; rm -rf -- "$temporary_directory"' EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=untrusted-validation' \
  -keyout "$temporary_directory/untrusted.key" -out "$temporary_directory/untrusted.crt" >/dev/null 2>&1
for endpoint in api.lab.test:443 registry.lab.test:5000; do
  host=${endpoint%:*}
  if printf '' | openssl s_client -connect "$endpoint" -servername "$host" \
    -CAfile "$temporary_directory/untrusted.crt" -verify_return_error -brief \
    >/dev/null 2>&1; then
    echo 'untrusted TLS authority was accepted' >&2; exit 1
  fi
done
k -n default create secret generic "$probe_name" \
  --from-literal=probe=validation-marker >/dev/null
k -n kube-system exec etcd-cp-1 -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt \
  --key=/etc/kubernetes/pki/etcd/healthcheck-client.key \
  get "/registry/secrets/default/$probe_name" -w json \
  | python3 -c 'import base64,json,sys; rows=json.load(sys.stdin)["kvs"]; assert len(rows)==1; value=base64.b64decode(rows[0]["value"]); assert value.startswith(b"k8s:enc:secretbox:v1:") and b"validation-marker" not in value'

k get services -A -o json | python3 -c '
import json,sys
services=json.load(sys.stdin)["items"]
exposed={(item["metadata"]["namespace"],item["metadata"]["name"]):item for item in services if item["spec"].get("type") in {"NodePort","LoadBalancer"}}
gateways=[item for (namespace,name),item in exposed.items() if namespace=="envoy-gateway-system" and name.startswith("envoy-envoy-gateway-system-platform-")]
assert len(gateways)==1, sorted(exposed)
assert set(exposed)=={("envoy-gateway-system",gateways[0]["metadata"]["name"]),("monitoring","opentelemetry-collector-build-host")}, sorted(exposed)
gateway=gateways[0]
assert gateway["spec"]["type"]=="LoadBalancer"
assert gateway["status"]["loadBalancer"]["ingress"][0]["ip"]=="10.20.0.100"
collector=exposed[("monitoring","opentelemetry-collector-build-host")]
assert collector["spec"]["type"]=="NodePort"
assert {port["nodePort"] for port in collector["spec"]["ports"]}=={30418}
'
for cp in 31 32 33; do
  ssh -o BatchMode=yes -i ${HOME}/.ssh/lap_lab_ed25519 "ubuntu@10.20.0.$cp" \
    sudo -n grep -q -- --encryption-provider-config=/etc/kubernetes/encryption-config.yaml \
    /etc/kubernetes/manifests/kube-apiserver.yaml
done
echo 'RBAC, bearer auth, unsafe input, encryption configuration and service exposure passed.'
