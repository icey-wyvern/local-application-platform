#!/bin/sh
set -eu

if [ "$#" -ne 8 ]; then
    echo "usage: $0 HELM KUBECONFIG VERSION MANIFESTS MODE WORKER1 WORKER2 WORKER3" >&2
    exit 2
fi

helm_binary=$1
kubeconfig=$2
version=$3
manifests=$4
mode=$5
shift 5
expected_workers="$*"
namespace=longhorn-validation
longhorn_namespace=longhorn-system
kubectl_binary="$HOME/.local/bin/kubectl"
uv_binary=$(command -v uv 2>/dev/null || printf '%s/.local/bin/uv' "$HOME")
temporary_directory=$(mktemp -d)
failure_node=
node_cordoned=false
volume_name=

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

restore_node() {
    if [ "$node_cordoned" = true ]; then
        kubectl_cmd uncordon "$failure_node" >/dev/null 2>&1 || true
        node_cordoned=false
    fi
}

remove_validation_resources() {
    kubectl_cmd delete --kustomize "$manifests" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    if kubectl_cmd get namespace "$namespace" >/dev/null 2>&1; then
        kubectl_cmd wait --for=delete "namespace/$namespace" --timeout=5m >/dev/null
    fi
}

cleanup() {
    restore_node
    remove_validation_resources
    rm -rf "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

release=$(
    "$helm_binary" --kubeconfig "$kubeconfig" list --namespace "$longhorn_namespace" \
        --filter '^longhorn$' --output json
)
printf '%s' "$release" | grep -q "\"chart\":\"longhorn-${version}\""

for daemonset in longhorn-manager longhorn-csi-plugin; do
    kubectl_cmd --namespace "$longhorn_namespace" rollout status "daemonset/$daemonset" --timeout=5m
    test "$(kubectl_cmd --namespace "$longhorn_namespace" get daemonset "$daemonset" -o jsonpath='{.status.numberReady}')" = "3"
done
test "$(kubectl_cmd --namespace "$longhorn_namespace" get daemonset -l longhorn.io/component=engine-image -o jsonpath='{.items[0].status.numberReady}')" = "3"
for deployment in longhorn-driver-deployer longhorn-ui csi-attacher csi-provisioner csi-resizer csi-snapshotter; do
    kubectl_cmd --namespace "$longhorn_namespace" rollout status "deployment/$deployment" --timeout=5m
done
kubectl_cmd --namespace "$longhorn_namespace" wait --for=condition=Ready pod --all --timeout=5m

test "$(kubectl_cmd --namespace "$longhorn_namespace" get setting v1-data-engine -o jsonpath='{.value}')" = "true"
test "$(kubectl_cmd --namespace "$longhorn_namespace" get setting v2-data-engine -o jsonpath='{.value}')" = "false"

kubectl_cmd --namespace "$longhorn_namespace" get nodes.longhorn.io -o json >"$temporary_directory/nodes.json"
"$uv_binary" run python -c \
    'import json,pathlib,sys; data=json.loads(pathlib.Path(sys.argv[1]).read_text()); nodes=data["items"]; expected=set(sys.argv[2:]); assert {n["metadata"]["name"] for n in nodes}==expected; assert all(all(c["status"]=="True" for c in n["status"]["conditions"] if c["type"] in {"Ready","Schedulable","RequiredPackages","Multipathd","NFSClientInstalled","KernelModulesLoaded","MountPropagation"}) for n in nodes); assert all(len(n["status"]["diskStatus"])==1 for n in nodes); disks=[next(iter(n["status"]["diskStatus"].values())) for n in nodes]; specs=[next(iter(n["spec"]["disks"].values())) for n in nodes]; assert all(d["diskPath"]=="/var/lib/longhorn" and d["storageMaximum"]>0 for d in disks); assert all(abs(s["storageReserved"]-(d["storageMaximum"]*30//100))<=1 for s,d in zip(specs,disks,strict=True)); assert all(all(c["status"]=="True" for c in d["conditions"] if c["type"] in {"Ready","Schedulable"}) for d in disks)' \
    "$temporary_directory/nodes.json" "$@"

kubectl_cmd get storageclass longhorn -o json >"$temporary_directory/storageclass.json"
"$uv_binary" run python -c \
    'import json,pathlib,sys; s=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert s["provisioner"]=="driver.longhorn.io"; assert s["parameters"]["numberOfReplicas"]=="3"; assert s["parameters"]["dataEngine"]=="v1"; assert s["parameters"]["fsType"]=="ext4"; assert s["reclaimPolicy"]=="Delete"; assert s["volumeBindingMode"]=="WaitForFirstConsumer"; assert s["allowVolumeExpansion"] is True; assert s["metadata"]["annotations"]["storageclass.kubernetes.io/is-default-class"]=="true"' \
    "$temporary_directory/storageclass.json"

remove_validation_resources
kubectl_cmd apply --server-side --field-manager=lap-validation --kustomize "$manifests" >/dev/null
kubectl_cmd --namespace "$namespace" wait --for=jsonpath='{.status.phase}'=Bound pvc/data --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status deployment/longhorn-validation --timeout=5m

pod=$(kubectl_cmd --namespace "$namespace" get pod -l app.kubernetes.io/name=longhorn-validation -o jsonpath='{.items[0].metadata.name}')
pod_uid=$(kubectl_cmd --namespace "$namespace" get pod "$pod" -o jsonpath='{.metadata.uid}')
volume_name=$(kubectl_cmd --namespace "$namespace" get pvc data -o jsonpath='{.spec.volumeName}')
payload="longhorn-${version}-$(date +%s)"
kubectl_cmd --namespace "$namespace" exec "$pod" -- sh -c 'printf "%s\n" "$1" >/data/payload && sync' sh "$payload"
test "$(kubectl_cmd --namespace "$namespace" exec "$pod" -- cat /data/payload)" = "$payload"

kubectl_cmd --namespace "$namespace" delete pod "$pod" --wait=false >/dev/null
kubectl_cmd --namespace "$namespace" wait --for=delete "pod/$pod" --timeout=3m
kubectl_cmd --namespace "$namespace" rollout status deployment/longhorn-validation --timeout=5m
pod=$(kubectl_cmd --namespace "$namespace" get pod -l app.kubernetes.io/name=longhorn-validation -o jsonpath='{.items[0].metadata.name}')
test "$(kubectl_cmd --namespace "$namespace" get pod "$pod" -o jsonpath='{.metadata.uid}')" != "$pod_uid"
test "$(kubectl_cmd --namespace "$namespace" exec "$pod" -- cat /data/payload)" = "$payload"

assert_volume_healthy() {
    for attempt in $(seq 1 120); do
        kubectl_cmd --namespace "$longhorn_namespace" get volumes.longhorn.io "$volume_name" -o json >"$temporary_directory/volume.json"
        kubectl_cmd --namespace "$longhorn_namespace" get replicas.longhorn.io -o json >"$temporary_directory/replicas.json"
        if "$uv_binary" run python -c \
            'import json,pathlib,sys; volume=json.loads(pathlib.Path(sys.argv[1]).read_text()); replicas=json.loads(pathlib.Path(sys.argv[2]).read_text()); selected=[r for r in replicas["items"] if r["spec"]["volumeName"]==volume["metadata"]["name"]]; assert volume["status"]["robustness"]=="healthy"; assert len(selected)==3; assert {r["spec"]["nodeID"] for r in selected}==set(sys.argv[3:]); assert all(r["status"]["currentState"]=="running" for r in selected)' \
            "$temporary_directory/volume.json" "$temporary_directory/replicas.json" "$@"; then
            return
        fi
        sleep 2
    done
    echo "Longhorn volume did not become healthy with one replica per worker" >&2
    return 1
}

assert_volume_healthy "$@"

if [ "$mode" = "--failure" ]; then
    failure_node=$(kubectl_cmd --namespace "$namespace" get pod "$pod" -o jsonpath='{.spec.nodeName}')
    kubectl_cmd cordon "$failure_node" >/dev/null
    node_cordoned=true
    kubectl_cmd drain "$failure_node" --ignore-daemonsets --delete-emptydir-data --force \
        --pod-selector=app.kubernetes.io/name=longhorn-validation --timeout=5m >/dev/null
    kubectl_cmd --namespace "$namespace" rollout status deployment/longhorn-validation --timeout=5m
    pod=$(kubectl_cmd --namespace "$namespace" get pod -l app.kubernetes.io/name=longhorn-validation -o jsonpath='{.items[0].metadata.name}')
    recovery_node=$(kubectl_cmd --namespace "$namespace" get pod "$pod" -o jsonpath='{.spec.nodeName}')
    test "$recovery_node" != "$failure_node"
    test "$(kubectl_cmd --namespace "$namespace" exec "$pod" -- cat /data/payload)" = "$payload"
    restore_node
    kubectl_cmd wait --for=condition=Ready "node/$failure_node" --timeout=3m
    assert_volume_healthy "$@"
    echo "Workload and volume recovered from ${failure_node} on ${recovery_node}."
fi

remove_validation_resources
for attempt in $(seq 1 120); do
    if ! kubectl_cmd get pv "$volume_name" >/dev/null 2>&1 && \
       ! kubectl_cmd --namespace "$longhorn_namespace" get volumes.longhorn.io "$volume_name" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
! kubectl_cmd get pv "$volume_name" >/dev/null 2>&1
! kubectl_cmd --namespace "$longhorn_namespace" get volumes.longhorn.io "$volume_name" >/dev/null 2>&1
kubectl_cmd --namespace "$longhorn_namespace" get replicas.longhorn.io -o json | "$uv_binary" run python -c \
    'import json,sys; name=sys.argv[1]; assert all(r["spec"]["volumeName"]!=name for r in json.load(sys.stdin)["items"])' "$volume_name"
! kubectl_cmd get namespace "$namespace" >/dev/null 2>&1
test "$(kubectl_cmd --namespace "$longhorn_namespace" get daemonset longhorn-manager -o jsonpath='{.status.numberReady}')" = "3"
test "$(kubectl_cmd --namespace "$longhorn_namespace" get daemonset longhorn-csi-plugin -o jsonpath='{.status.numberReady}')" = "3"
test "$(kubectl_cmd get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')" = "6"

trap - EXIT HUP INT TERM
rm -rf "$temporary_directory"
echo "Longhorn storage validation passed (${mode#--}); removed volume ${volume_name}."
