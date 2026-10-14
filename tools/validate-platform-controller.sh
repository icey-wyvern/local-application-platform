#!/usr/bin/env bash
set -euo pipefail

kubectl_bin="${1:-${HOME}/.local/bin/kubectl}"
kubeconfig="${2:-${HOME}/.kube/lap-admin.conf}"
image="${3:?immutable runtime fixture image is required}"
lab_root_ca="${4:-.generated/pki/lab-root-ca.crt}"
suffix="$(date +%s | tail -c 9)"
name="controller-validation-$suffix"
namespace="app-$name"
authorized_client="controller-authorized-$suffix"
unauthorized_client="controller-unauthorized-$suffix"
release_a=00000000-0000-4000-8000-000000000001
release_b=00000000-0000-4000-8000-000000000002
busybox_image=docker.io/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e
application_label=platform.lab.test/application

[[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || {
  echo 'runtime fixture image must be digest-pinned' >&2
  exit 1
}
[[ -s "$lab_root_ca" ]] || { echo 'lab root CA is required' >&2; exit 1; }

cleanup() {
  "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace envoy-gateway-system \
    delete pod "$authorized_client" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace default \
    delete pod "$unauthorized_client" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  "$kubectl_bin" --kubeconfig "$kubeconfig" delete application "$name" \
    --ignore-not-found --wait=true >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

apply_application() {
  release_id=$1
  cat <<EOF | "$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
    --field-manager=lap-validation --validate=strict -f - >/dev/null
apiVersion: platform.lab.test/v1alpha1
kind: Application
metadata:
  name: $name
spec:
  releaseId: $release_id
  image: $image
  port: 8080
  healthPath: /health
  replicas: 2
EOF
}

wait_ready() {
  expected_release=$1
  attempts=0
  until "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" \
    get deployment application >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 60 ] || { echo 'runtime Deployment was not created' >&2; exit 1; }
    sleep 1
  done
  "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" \
    rollout status deployment/application --timeout=5m >/dev/null
  attempts=0
  while [ "$attempts" -lt 60 ]; do
    document=$("$kubectl_bin" --kubeconfig "$kubeconfig" get application "$name" -o json)
    if printf '%s' "$document" | python3 -c \
      'import json,sys; d=json.load(sys.stdin); assert d["status"]["observedGeneration"] == d["metadata"]["generation"]; assert d["status"]["phase"] == "Ready"; assert d["status"]["readyReplicas"] == 2; assert d["status"]["activeReleaseId"] == sys.argv[1]; conditions={c["type"]:c for c in d["status"]["conditions"]}; assert all(conditions[name]["status"] == "True" for name in ("RuntimeReady","RouteReady","Healthy","Available"))' \
      "$expected_release" 2>/dev/null; then
      return
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  echo 'Application did not report Ready before timeout' >&2
  exit 1
}

assert_invalid() {
  description=$1
  manifest=$2
  if printf '%s\n' "$manifest" | "$kubectl_bin" --kubeconfig "$kubeconfig" \
      apply --dry-run=server --validate=strict -f - >/dev/null 2>&1; then
    echo "invalid $description Application was accepted" >&2
    exit 1
  fi
}

base="apiVersion: platform.lab.test/v1alpha1
kind: Application
metadata:
  name: schema-invalid
spec:
  releaseId: $release_a
  image: $image
  port: 8080
  healthPath: /health
  replicas: 2"
assert_invalid replicas "$(printf '%s' "$base" | sed 's/replicas: 2/replicas: 1/')"
assert_invalid port "$(printf '%s' "$base" | sed 's/port: 8080/port: 80/')"
assert_invalid image "$(printf '%s' "$base" | sed "s#image: .*#image: registry.lab.test:5000/apps/schema-invalid:latest#")"
assert_invalid healthPath "$(printf '%s' "$base" | sed 's#healthPath: /health#healthPath: //invalid#')"
assert_invalid unknown-field "$base
  unknown: true"

apply_application "$release_a"
wait_ready "$release_a"

"$kubectl_bin" --kubeconfig "$kubeconfig" get namespace "$namespace" -o json |
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"'
for resource in \
  serviceaccount/application \
  resourcequota/application \
  limitrange/application \
  secret/registry-pull \
  deployment/application \
  service/application \
  httproute.gateway.networking.k8s.io/application \
  networkpolicy/default-deny \
  networkpolicy/allow-dns \
  networkpolicy/allow-envoy-ingress \
  networkpolicy/allow-public-https; do
  "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get "$resource" >/dev/null
done

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get deployment application -o json |
  python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["spec"]; c=s["template"]["spec"]["containers"][0]; assert s["replicas"] == 2; assert s["strategy"]["rollingUpdate"] == {"maxSurge":1,"maxUnavailable":0}; assert s["progressDeadlineSeconds"] == 300; assert c["resources"] == {"requests":{"cpu":"100m","memory":"128Mi"},"limits":{"cpu":"1","memory":"512Mi"}}; assert c["readinessProbe"]["httpGet"]["path"] == "/health"; assert c["livenessProbe"]["httpGet"]["path"] == "/health"; assert s["template"]["spec"]["securityContext"]["runAsNonRoot"] is True; assert c["securityContext"]["allowPrivilegeEscalation"] is False'
nodes=$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get pods \
  -l "$application_label=$name" -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u | wc -l)
[ "$nodes" -eq 2 ] || { echo 'application replicas were not spread across two workers' >&2; exit 1; }

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get serviceaccount application -o json |
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["automountServiceAccountToken"] is False; assert d["imagePullSecrets"] == [{"name":"registry-pull"}]'
test "$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get secret registry-pull -o jsonpath='{.type}')" = kubernetes.io/dockerconfigjson
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get httproute application -o json |
  python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["spec"]; assert s["hostnames"] == [sys.argv[1]+".apps.lab.test"]; assert s["parentRefs"] == [{"group":"gateway.networking.k8s.io","kind":"Gateway","name":"platform","namespace":"envoy-gateway-system","sectionName":"https"}]; backend=s["rules"][0]["backendRefs"][0]; assert backend["group"] == "" and backend["kind"] == "Service" and backend["name"] == "application" and backend["port"] == 8080; conditions={c["type"]:c for p in d["status"]["parents"] for c in p["conditions"]}; assert conditions["Accepted"]["status"] == "True" and conditions["ResolvedRefs"]["status"] == "True"' "$name"
hostname="$name.apps.lab.test"
gateway_ip=10.20.0.100
curl --noproxy '*' --silent --show-error --fail --cacert "$lab_root_ca" \
  --resolve "$hostname:443:$gateway_ip" "https://$hostname/health" | grep -qx ok

cat <<EOF | "$kubectl_bin" --kubeconfig "$kubeconfig" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $authorized_client
  namespace: envoy-gateway-system
  labels:
    app.kubernetes.io/component: proxy
    gateway.envoyproxy.io/owning-gateway-name: platform
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: client
      image: $busybox_image
      command: [sleep, "600"]
      resources:
        requests: {cpu: 10m, memory: 8Mi}
        limits: {cpu: 100m, memory: 32Mi}
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: {drop: [ALL]}
        readOnlyRootFilesystem: true
EOF
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace envoy-gateway-system \
  wait --for=condition=Ready "pod/$authorized_client" --timeout=2m >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace envoy-gateway-system exec "$authorized_client" -- \
  wget -qO- "http://application.$namespace.svc.cluster.local:8080/health" | grep -qx ok

cat <<EOF | "$kubectl_bin" --kubeconfig "$kubeconfig" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $unauthorized_client
  namespace: default
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: client
      image: $busybox_image
      command: [sleep, "600"]
      resources:
        requests: {cpu: 10m, memory: 8Mi}
        limits: {cpu: 100m, memory: 32Mi}
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: {drop: [ALL]}
        readOnlyRootFilesystem: true
EOF
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace default \
  wait --for=condition=Ready "pod/$unauthorized_client" --timeout=2m >/dev/null
if "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace default exec "$unauthorized_client" -- \
    wget -T 3 -qO- "http://application.$namespace.svc.cluster.local:8080/health" >/dev/null 2>&1; then
  echo 'unauthorized cross-namespace ingress was allowed' >&2
  exit 1
fi

workload_pod=$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" \
  get pod -l "$application_label=$name" -o jsonpath='{.items[0].metadata.name}')
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" exec "$workload_pod" -- \
  nslookup kubernetes.default.svc.cluster.local >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" exec "$workload_pod" -- \
  nc -z -w 5 example.com 443
if "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" exec "$workload_pod" -- \
    nc -z -w 3 example.com 80 >/dev/null 2>&1; then
  echo 'public plaintext HTTP egress was allowed' >&2
  exit 1
fi
if "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" exec "$workload_pod" -- \
    nc -z -w 3 kubernetes.default.svc.cluster.local 443 >/dev/null 2>&1; then
  echo 'internal Service-network egress was allowed' >&2
  exit 1
fi

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" patch deployment application \
  --type=merge --patch '{"spec":{"replicas":1}}' >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" delete service application >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" patch httproute application \
  --type=json --patch='[{"op":"replace","path":"/spec/hostnames/0","value":"drifted.apps.lab.test"}]' >/dev/null
# Interrupt reconciliation while the desired Deployment, Service and route differ.
controller_pod=$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  get pod -l app.kubernetes.io/name=platform-controller -o jsonpath='{.items[0].metadata.name}')
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  delete pod "$controller_pod" --wait=true >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  rollout status deployment/platform-controller --timeout=5m >/dev/null
attempts=0
until [ "$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get deployment application -o jsonpath='{.spec.replicas}' 2>/dev/null)" = 2 ] \
  && "$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get service application >/dev/null 2>&1 \
  && [ "$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get httproute application -o jsonpath='{.spec.hostnames[0]}' 2>/dev/null)" = "$hostname" ]; do
  attempts=$((attempts + 1))
  [ "$attempts" -lt 60 ] || { echo 'controller did not repair runtime drift' >&2; exit 1; }
  sleep 1
done
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" rollout status deployment/application --timeout=5m >/dev/null
old_route_uid=$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get httproute application -o jsonpath='{.metadata.uid}')
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" delete httproute application >/dev/null
attempts=0
until new_route_uid=$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" get httproute application -o jsonpath='{.metadata.uid}' 2>/dev/null) \
  && [ -n "$new_route_uid" ] && [ "$new_route_uid" != "$old_route_uid" ]; do
  attempts=$((attempts + 1))
  [ "$attempts" -lt 60 ] || { echo 'controller did not recreate deleted HTTPRoute' >&2; exit 1; }
  sleep 1
done
wait_ready "$release_a"

apply_application "$release_b"
wait_ready "$release_b"

deployment_version=$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" \
  get deployment application -o jsonpath='{.metadata.resourceVersion}')
route_version=$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" \
  get httproute application -o jsonpath='{.metadata.resourceVersion}')
controller_pod=$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  get pod -l app.kubernetes.io/name=platform-controller -o jsonpath='{.items[0].metadata.name}')
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  delete pod "$controller_pod" --wait=true >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  rollout status deployment/platform-controller --timeout=5m >/dev/null
sleep 5
test "$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" \
  get deployment application -o jsonpath='{.metadata.resourceVersion}')" = "$deployment_version"
test "$("$kubectl_bin" --kubeconfig "$kubeconfig" --namespace "$namespace" \
  get httproute application -o jsonpath='{.metadata.resourceVersion}')" = "$route_version"
wait_ready "$release_b"

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace envoy-gateway-system \
  delete pod "$authorized_client" --wait=true >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace default \
  delete pod "$unauthorized_client" --wait=true >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" delete application "$name" --wait=true >/dev/null
trap - EXIT HUP INT TERM
if "$kubectl_bin" --kubeconfig "$kubeconfig" get namespace "$namespace" >/dev/null 2>&1; then
  echo 'runtime Namespace remained after Application finalization' >&2
  exit 1
fi

echo 'Runtime resources, readiness, isolation, drift repair, restart stability, and deletion passed.'
