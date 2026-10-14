#!/bin/sh
set -eu

kubectl=${1:?kubectl path is required}
kubeconfig=${2:?kubeconfig is required}
token_file=${3:?platform API token file is required}
repository=${4:-}
local_port=${5:-18081}
gateway_ip=${6:-10.20.0.100}
namespace=platform-system
base_url="http://127.0.0.1:$local_port/api/v1"
forward_pid=
application_name=
failure_application_name=

cleanup() {
    if [ -n "$forward_pid" ]; then
        kill "$forward_pid" 2>/dev/null || true
        wait "$forward_pid" 2>/dev/null || true
    fi
    if [ -n "$application_name" ]; then
        "$kubectl" --kubeconfig "$kubeconfig" delete application "$application_name" \
            --ignore-not-found --wait=true >/dev/null 2>&1 || true
    fi
    if [ -n "$failure_application_name" ]; then
        "$kubectl" --kubeconfig "$kubeconfig" delete application "$failure_application_name" \
            --ignore-not-found --wait=true >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT HUP INT TERM

token=$(tr -d '\n' <"$token_file")
"$kubectl" --kubeconfig "$kubeconfig" --namespace "$namespace" \
    port-forward service/platform-api "$local_port:8080" \
    >/tmp/lap-platform-api-build-dispatch-forward.log 2>&1 &
forward_pid=$!
attempts=0
while ! nc -z 127.0.0.1 "$local_port" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 30 ] || {
        cat /tmp/lap-platform-api-build-dispatch-forward.log >&2
        exit 1
    }
    sleep 1
done

api_get() {
    curl --silent --show-error --fail \
        --header "Authorization: Bearer $token" "$base_url$1"
}

submit() {
    revision=$1
    key=${2:-$(python3 -c 'import uuid; print(uuid.uuid4())')}
    payload=$(python3 -c \
        'import json,sys; print(json.dumps({"repositoryUrl":sys.argv[1],"revision":{"type":"branch","value":sys.argv[2]}}))' \
        "$repository" "$revision")
    curl --silent --show-error --fail \
        --header "Authorization: Bearer $token" \
        --header "Idempotency-Key: $key" \
        --header 'Content-Type: application/json' \
        --data "$payload" "$base_url/deployments"
}

poll_operation() {
    operation_id=$1
    limit=$2
    attempts=0
    while [ "$attempts" -lt "$limit" ]; do
        response=$(api_get "/operations/$operation_id")
        status=$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
        if [ "$status" = succeeded ] || [ "$status" = failed ]; then
            printf '%s' "$response"
            return
        fi
        attempts=$((attempts + 1))
        sleep 2
    done
    echo "operation $operation_id did not finish" >&2
    exit 1
}

ready_replicas=$("$kubectl" --kubeconfig "$kubeconfig" --namespace "$namespace" \
    get deployment platform-api -o jsonpath='{.status.readyReplicas}')
[ "$ready_replicas" = 2 ] || { echo 'platform API does not have two ready replicas' >&2; exit 1; }

if [ -z "$repository" ]; then
    if [ "${LAP_VALIDATE_POD_RECOVERY:-0}" = 1 ]; then
        repository=https://github.com/kubernetes/kubernetes.git
    else
        repository=https://github.com/octocat/Hello-World.git
    fi
    submitted=$(submit master)
    operation_id=$(printf '%s' "$submitted" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    build_id=$(printf '%s' "$submitted" | python3 -c 'import json,sys; print(json.load(sys.stdin)["buildId"])')
    if [ "${LAP_VALIDATE_POD_RECOVERY:-0}" = 1 ]; then
        claimed_pod=
        attempts=0
        while [ -z "$claimed_pod" ] && [ "$attempts" -lt 100 ]; do
            claimed_pod=$("$kubectl" --kubeconfig "$kubeconfig" --namespace "$namespace" \
                logs --selector app.kubernetes.io/name=platform-api --prefix=true \
                --since=2m --tail=-1 2>/dev/null \
                | grep "$operation_id" | grep 'operation claimed' | head -1 \
                | sed -n 's#^\[pod/\([^/]\{1,253\}\)/.*#\1#p' || true)
            attempts=$((attempts + 1))
            [ -n "$claimed_pod" ] || sleep 0.1
        done
        [ -n "$claimed_pod" ] || { echo 'could not identify operation owner' >&2; exit 1; }
        "$kubectl" --kubeconfig "$kubeconfig" --namespace "$namespace" \
            delete pod "$claimed_pod" --wait=true >/dev/null
        "$kubectl" --kubeconfig "$kubeconfig" --namespace "$namespace" \
            rollout status deployment/platform-api --timeout=5m >/dev/null
    fi
    terminal=$(poll_operation "$operation_id" 90)
    printf '%s' "$terminal" | python3 -c \
        'import json,sys; value=json.load(sys.stdin); assert value["status"] == "failed", value; assert value["failureCode"] == "PLATFORM_CONFIG_MISSING", value'
    if [ "${LAP_VALIDATE_POD_RECOVERY:-0}" = 1 ]; then
        printf '%s' "$terminal" | python3 -c \
            'import json,sys; value=json.load(sys.stdin); assert value["attempts"] >= 2, value'
        counts=$("$kubectl" --kubeconfig "$kubeconfig" --namespace platform-database \
            exec statefulset/postgresql -- psql -U postgres -d platform -At -c \
            "SELECT (SELECT count(*) FROM builds WHERE id='$build_id'), (SELECT count(*) FROM releases WHERE build_id='$build_id'), (SELECT count(*) FROM deployments WHERE operation_id='$operation_id')" 2>/dev/null)
        [ "$counts" = '1|0|0' ] || {
            echo "unexpected Build/Release/Deployment counts after Pod loss: $counts" >&2
            exit 1
        }
    fi
    build=$(api_get "/builds/$build_id")
    printf '%s' "$build" | python3 -c \
        'import json,sys; value=json.load(sys.stdin); assert value["status"] == "failed", value; assert value["releaseId"] is None, value'
    echo 'Two-replica live mTLS dispatch and terminal no-Release validation passed.'
    echo 'Set BUILD_AGENT_TEST_REPOSITORY to the disposable fixture URL for full success/policy validation.'
    exit 0
fi

idempotency_key=$(python3 -c 'import uuid; print(uuid.uuid4())')
submitted=$(submit success "$idempotency_key")
operation_id=$(printf '%s' "$submitted" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
build_id=$(printf '%s' "$submitted" | python3 -c 'import json,sys; print(json.load(sys.stdin)["buildId"])')
terminal=$(poll_operation "$operation_id" 480)
printf '%s' "$terminal" | python3 -c \
    'import json,sys; value=json.load(sys.stdin); assert value["status"] == "succeeded", value'
build=$(api_get "/builds/$build_id")
release_id=$(printf '%s' "$build" | python3 -c \
    'import json,re,sys; value=json.load(sys.stdin); assert value["status"] == "succeeded", value; assert re.fullmatch("[0-9a-f]{40}", value["resolvedCommit"]), value; assert value["releaseId"], value; print(value["releaseId"])')
release=$(api_get "/releases/$release_id")
printf '%s\n%s' "$build" "$release" | python3 -c \
    'import json,re,sys; build,release=(json.loads(line) for line in sys.stdin); assert release["gitCommit"] == build["resolvedCommit"]; assert re.fullmatch("sha256:[0-9a-f]{64}", release["imageDigest"]); assert release["imageReference"].endswith("@" + release["imageDigest"])'
application_id=$(printf '%s' "$release" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["applicationId"])')
application_name=$(api_get /applications | python3 -c \
    'import json,sys; values=json.load(sys.stdin); matches=[v["name"] for v in values if v["id"] == sys.argv[1]]; assert len(matches) == 1; print(matches[0])' \
    "$application_id")
deployment_id=$(printf '%s' "$terminal" | python3 -c \
    'import json,sys; value=json.load(sys.stdin); assert value["deploymentId"]; print(value["deploymentId"])')
deployment=$(api_get "/deployments/$deployment_id")
printf '%s\n%s' "$release" "$deployment" | python3 -c \
    'import json,sys; release,deployment=(json.loads(line) for line in sys.stdin); assert deployment["desiredReleaseId"] == release["id"]; assert deployment["status"] == "healthy"; assert deployment["completedAt"]'

cr=$("$kubectl" --kubeconfig "$kubeconfig" get application "$application_name" -o json \
    | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin),separators=(",",":")))')
printf '%s\n%s' "$release" "$cr" | python3 -c \
    'import json,sys; release,cr=(json.loads(line) for line in sys.stdin); assert cr["spec"] == {"releaseId":release["id"],"image":release["imageReference"],"port":release["port"],"healthPath":release["healthPath"],"replicas":2}; status=cr["status"]; assert status["observedGeneration"] == cr["metadata"]["generation"]; assert status["activeReleaseId"] == release["id"]; assert status["desiredImage"] == status["currentImage"] == release["imageReference"]; assert status["readyReplicas"] == 2; assert status["phase"] == "Ready"; conditions={c["type"]:c for c in status["conditions"]}; assert all(conditions[name]["status"] == "True" for name in ("Reconciled","RuntimeReady","RouteReady","Healthy","Available"))'
cr_uid=$(printf '%s' "$cr" | python3 -c 'import json,sys; print(json.load(sys.stdin)["metadata"]["uid"])')
cr_generation=$(printf '%s' "$cr" | python3 -c 'import json,sys; print(json.load(sys.stdin)["metadata"]["generation"])')

replayed=$(submit success "$idempotency_key")
printf '%s\n%s' "$submitted" "$replayed" | python3 -c \
    'import json,sys; first,replay=(json.loads(line) for line in sys.stdin); assert first["id"] == replay["id"]; assert first["buildId"] == replay["buildId"]; assert replay["status"] == "succeeded"'
test "$("$kubectl" --kubeconfig "$kubeconfig" get application "$application_name" -o jsonpath='{.metadata.uid}')" = "$cr_uid"
test "$("$kubectl" --kubeconfig "$kubeconfig" get application "$application_name" -o jsonpath='{.metadata.generation}')" = "$cr_generation"

runtime_namespace="app-$application_name"
test "$("$kubectl" --kubeconfig "$kubeconfig" get namespace "$runtime_namespace" -o jsonpath='{.metadata.ownerReferences[0].uid}')" = "$cr_uid"
test "$("$kubectl" --kubeconfig "$kubeconfig" --namespace "$runtime_namespace" get deployment application -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}')" = platform-controller
test "$("$kubectl" --kubeconfig "$kubeconfig" --namespace "$runtime_namespace" get deployment application -o jsonpath='{.status.readyReplicas}')" = 2
test -n "$("$kubectl" --kubeconfig "$kubeconfig" --namespace "$runtime_namespace" get service application -o jsonpath='{.spec.clusterIP}')"
route=$("$kubectl" --kubeconfig "$kubeconfig" --namespace "$runtime_namespace" get httproute application -o json | python3 -c 'import json,sys; route=json.load(sys.stdin); conditions=[c for p in route["status"]["parents"] for c in p["conditions"]]; values={c["type"]:c["status"] for c in conditions}; assert values["Accepted"] == values["ResolvedRefs"] == "True"; print("ready")')
test "$route" = ready
health_path=$(printf '%s' "$release" | python3 -c 'import json,sys; print(json.load(sys.stdin)["healthPath"])')
curl --silent --show-error --fail --cacert .generated/pki/lab-root-ca.crt \
    --resolve "$application_name.apps.lab.test:443:$gateway_ip" \
    "https://$application_name.apps.lab.test$health_path" >/dev/null
test "$("$kubectl" --kubeconfig "$kubeconfig" auth can-i create applications.platform.lab.test --as system:serviceaccount:platform-system:platform-api)" = yes
test "$("$kubectl" --kubeconfig "$kubeconfig" auth can-i patch applications.platform.lab.test --subresource=status --as system:serviceaccount:platform-system:platform-api)" = no
test "$("$kubectl" --kubeconfig "$kubeconfig" auth can-i create deployments.apps --as system:serviceaccount:platform-system:platform-api --namespace "$runtime_namespace")" = no
test "$("$kubectl" --kubeconfig "$kubeconfig" auth can-i create httproutes.gateway.networking.k8s.io --as system:serviceaccount:platform-system:platform-api --namespace "$runtime_namespace")" = no

if [ "${LAP_VALIDATE_APPLICATION_CR_ONLY:-0}" != 1 ]; then
    for fixture in vulnerable failed-build; do
        submitted=$(submit "$fixture")
        operation_id=$(printf '%s' "$submitted" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
        build_id=$(printf '%s' "$submitted" | python3 -c 'import json,sys; print(json.load(sys.stdin)["buildId"])')
        terminal=$(poll_operation "$operation_id" 480)
        build=$(api_get "/builds/$build_id")
        printf '%s\n%s' "$terminal" "$build" | python3 -c \
            'import json,sys; operation,build=(json.loads(line) for line in sys.stdin); assert operation["status"] == "failed", operation; assert build["status"] in {"failed","rejected"}, build; assert build["releaseId"] is None, build'
    done
fi

if [ "${LAP_VALIDATE_DEPLOYMENT_FAILURE:-0}" = 1 ]; then
    failed_submission=$(submit unhealthy)
    failed_operation_id=$(printf '%s' "$failed_submission" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    failed_build_id=$(printf '%s' "$failed_submission" | python3 -c 'import json,sys; print(json.load(sys.stdin)["buildId"])')
    failed_terminal=$(poll_operation "$failed_operation_id" 480)
    rollback_status=$(printf '%s' "$failed_terminal" | python3 -c 'import json,sys; print(json.load(sys.stdin)["failureResult"]["rollbackStatus"])')
    if [ -n "${LAP_VALIDATE_ROLLBACK_EXPECTED:-}" ]; then
        [ "$rollback_status" = "$LAP_VALIDATE_ROLLBACK_EXPECTED" ] || {
            echo "rollback state mismatch: expected $LAP_VALIDATE_ROLLBACK_EXPECTED, actual $rollback_status" >&2
            exit 1
        }
    fi
    failed_build=$(api_get "/builds/$failed_build_id")
    failed_release_id=$(printf '%s' "$failed_build" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert value["status"] == "succeeded", value; assert value["releaseId"]; print(value["releaseId"])')
    failed_release=$(api_get "/releases/$failed_release_id")
    failure_application_id=$(printf '%s' "$failed_release" | python3 -c 'import json,sys; print(json.load(sys.stdin)["applicationId"])')
    failure_application_name=$(api_get /applications | python3 -c 'import json,sys; values=json.load(sys.stdin); matches=[v["name"] for v in values if v["id"] == sys.argv[1]]; assert len(matches) == 1; print(matches[0])' "$failure_application_id")
    failed_deployment_id=$(printf '%s' "$failed_terminal" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert value["status"] == "failed", value; assert value["failureCode"] == "CONTROLLER_HTTPS_HEALTH_FAILED", value; assert value["deploymentId"] == value["failureResult"]["rollbackDeploymentId"], value; print(value["failureResult"]["failedDeploymentId"])')
    failed_deployment=$(api_get "/deployments/$failed_deployment_id")
    printf '%s' "$failed_deployment" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert value["status"] == "failed", value; assert value["failureCode"] == "CONTROLLER_HTTPS_HEALTH_FAILED", value; assert value["completedAt"], value'
    if [ "$rollback_status" = succeeded ]; then
        history=$(api_get "/applications/$failure_application_name/deployments")
        printf '%s\n%s\n%s' "$history" "$failed_deployment" "$failed_terminal" | python3 -c '
import json,sys
history,failed,operation=(json.loads(line) for line in sys.stdin)
attempts=[item for item in history if item["operationId"] == operation["id"]]
failures=[item for item in attempts if item["type"] == "release"]
rollbacks=[item for item in attempts if item["type"] == "automatic_rollback"]
assert len(failures) == 1 and failures[0]["status"] == "failed", attempts
assert len(rollbacks) == 1 and rollbacks[0]["status"] == "healthy", attempts
assert failures[0]["desiredReleaseId"] == failed["desiredReleaseId"]
assert rollbacks[0]["desiredReleaseId"] == sys.argv[1]
assert rollbacks[0]["rollbackOfDeploymentId"] == failures[0]["id"]
assert operation["failureResult"]["rollbackDeploymentId"] == rollbacks[0]["id"]
assert operation["failureResult"]["rollbackTargetReleaseId"] == sys.argv[1]
' "$release_id"
        restored_application=$(api_get "/applications/$failure_application_name")
        printf '%s' "$restored_application" | python3 -c \
            'import json,sys; app=json.load(sys.stdin); assert app["currentReleaseId"] == sys.argv[1]' "$release_id"
        restored_cr=$("$kubectl" --kubeconfig "$kubeconfig" get application "$failure_application_name" -o json \
            | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin),separators=(",",":")))')
        printf '%s\n%s' "$release" "$restored_cr" | python3 -c '
import json,sys
release,cr=(json.loads(line) for line in sys.stdin)
assert cr["spec"]["releaseId"] == release["id"]
assert cr["spec"]["image"] == release["imageReference"]
status=cr["status"]
assert status["observedGeneration"] == cr["metadata"]["generation"]
assert status["activeReleaseId"] == release["id"]
assert status["desiredImage"] == status["currentImage"] == release["imageReference"]
assert status["readyReplicas"] == 2 and status["phase"] == "Ready"
conditions={condition["type"]:condition["status"] for condition in status["conditions"]}
assert all(conditions[name] == "True" for name in ("Reconciled","RuntimeReady","RouteReady","Healthy","Available"))
'
        runtime_namespace="app-$failure_application_name"
        "$kubectl" --kubeconfig "$kubeconfig" --namespace "$runtime_namespace" \
            get service application >/dev/null
        "$kubectl" --kubeconfig "$kubeconfig" --namespace "$runtime_namespace" \
            get httproute application -o json | python3 -c \
            'import json,sys; route=json.load(sys.stdin); conditions={c["type"]:c["status"] for parent in route["status"]["parents"] for c in parent["conditions"]}; assert conditions["Accepted"] == conditions["ResolvedRefs"] == "True"'
        restored_health_path=$(printf '%s' "$release" | python3 -c 'import json,sys; print(json.load(sys.stdin)["healthPath"])')
        curl --silent --show-error --fail --cacert .generated/pki/lab-root-ca.crt \
            --resolve "$failure_application_name.apps.lab.test:443:$gateway_ip" \
            "https://$failure_application_name.apps.lab.test$restored_health_path" >/dev/null
    fi
fi

metrics=$(curl --silent --show-error --fail "http://127.0.0.1:$local_port/metrics")
for metric in \
    lap_platform_api_build_dispatches_total \
    lap_platform_api_build_results_total \
    lap_platform_api_release_creations_total \
    lap_platform_api_application_cr_actions_total \
    lap_platform_api_application_observation_duration_seconds \
    lap_platform_api_deployment_acceptance_duration_seconds \
    lap_platform_api_deployment_acceptance_stages_total
do
    printf '%s' "$metrics" | grep -Fq "# HELP $metric " || {
        echo "metric family $metric is not registered" >&2
        exit 1
    }
done
echo 'Full scheduler-to-build-agent and external HTTPS deployment acceptance validation passed.'
