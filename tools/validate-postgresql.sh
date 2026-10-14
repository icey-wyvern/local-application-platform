#!/bin/sh
set -eu

if [ "$#" -ne 10 ]; then
    echo "usage: $0 KUBECTL KUBECONFIG VERSION MANIFESTS ROOT_CA APP_FILE DATABASE APP_USER NAMESPACE SERVICE" >&2
    exit 2
fi

kubectl_binary=$1
kubeconfig=$2
version=$3
manifests=$4
root_ca=$5
application_file=$6
database=$7
application_user=$8
namespace=$9
service=${10}
validation_namespace=postgresql-validation
denied_namespace=postgresql-denied
longhorn_namespace=longhorn-system
temporary_directory=$(mktemp -d)
validation_table=postgresql_slice_validation
payload="postgresql-${version}-$(date +%s)"

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

remove_validation_resources() {
    kubectl_cmd delete --kustomize "$manifests" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    for item in "$validation_namespace" "$denied_namespace"; do
        if kubectl_cmd get namespace "$item" >/dev/null 2>&1; then
            kubectl_cmd wait --for=delete "namespace/$item" --timeout=5m >/dev/null 2>&1 || true
        fi
    done
}

cleanup() {
    remove_validation_resources
    rm -rf "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

test -s "$root_ca"
test -s "$application_file"
test "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.spec.type}')" = ClusterIP
test -z "$(kubectl_cmd --namespace "$namespace" get service "$service" -o jsonpath='{.spec.externalIPs}')"
kubectl_cmd --namespace "$namespace" wait --for=condition=Ready certificate/postgresql-server --timeout=3m
kubectl_cmd --namespace "$namespace" rollout status statefulset/postgresql --timeout=5m
test "$(kubectl_cmd --namespace "$namespace" exec postgresql-0 -- postgres --version | awk '{print $3}')" = "$version"
test "$(kubectl_cmd --namespace "$namespace" exec postgresql-0 -- psql -AtU postgres -d "$database" -c 'show data_checksums')" = on
test "$(kubectl_cmd --namespace "$namespace" exec postgresql-0 -- psql -AtU postgres -d "$database" -c 'show password_encryption')" = scram-sha-256
test "$(kubectl_cmd --namespace "$namespace" exec postgresql-0 -- psql -AtU postgres -d "$database" -c 'show ssl')" = on

kubectl_cmd --namespace "$namespace" get secret postgresql-tls \
    -o jsonpath='{.data.tls\.crt}' | base64 -d >"$temporary_directory/server-chain.crt"
openssl x509 -in "$temporary_directory/server-chain.crt" -noout \
    -checkend 5184000 -ext subjectAltName | grep -q 'DNS:\*.\?postgresql.platform-database.svc.cluster.local\|DNS:postgresql.platform-database.svc.cluster.local'
openssl verify -CAfile "$root_ca" -untrusted "$temporary_directory/server-chain.crt" \
    "$temporary_directory/server-chain.crt" >/dev/null

remove_validation_resources
kubectl_cmd apply --server-side --field-manager=lap-validation \
    --kustomize "$manifests" >/dev/null
kubectl_cmd --namespace "$validation_namespace" create configmap lab-root-ca \
    --from-file=ca.crt="$root_ca" --dry-run=client -o yaml |
    kubectl_cmd apply --server-side --field-manager=lap-validation -f - >/dev/null
kubectl_cmd --namespace "$validation_namespace" create secret generic postgresql-application \
    --from-literal=database="$database" --from-literal=username="$application_user" \
    --from-file=password="$application_file" --dry-run=client -o yaml |
    kubectl_cmd apply --server-side --field-manager=lap-validation -f - >/dev/null
kubectl_cmd --namespace "$validation_namespace" wait --for=condition=Ready \
    pod/postgresql-validator --timeout=5m
kubectl_cmd --namespace "$denied_namespace" wait --for=condition=Ready \
    pod/postgresql-denied --timeout=5m

client_exec() {
    kubectl_cmd --namespace "$validation_namespace" exec postgresql-validator -- "$@"
}

client_exec getent hosts "${service}.${namespace}.svc.cluster.local" >/dev/null
test "$(client_exec psql -Atc "select current_user || ':' || current_database()")" = "${application_user}:${database}"
test "$(client_exec psql -Atc "select ssl from pg_stat_ssl where pid = pg_backend_pid()")" = t
test "$(client_exec psql -Atc "select rolsuper or rolcreatedb or rolcreaterole or rolreplication from pg_roles where rolname = current_user")" = f
if client_exec env -u PGPASSWORD psql -w -Atc 'select 1' >/dev/null 2>&1; then
    echo "unauthenticated PostgreSQL connection unexpectedly succeeded" >&2
    exit 1
fi
if client_exec env PGPASSWORD=definitely-wrong psql -w -Atc 'select 1' >/dev/null 2>&1; then
    echo "wrong-password PostgreSQL connection unexpectedly succeeded" >&2
    exit 1
fi
if kubectl_cmd --namespace "$denied_namespace" exec postgresql-denied -- \
    sh -c "timeout 8 pg_isready -h ${service}.${namespace}.svc.cluster.local -p 5432" >/dev/null 2>&1; then
    echo "unauthorized validation identity reached PostgreSQL" >&2
    exit 1
fi

client_exec psql -v ON_ERROR_STOP=1 -c \
    "create table if not exists ${validation_table} (id text primary key, payload text not null)" >/dev/null
client_exec psql -v ON_ERROR_STOP=1 -c \
    "insert into ${validation_table} values ('$payload', '$payload')" >/dev/null
test "$(client_exec psql -At -c "select payload from ${validation_table} where id = '$payload'")" = "$payload"

pvc=data-postgresql-0
test "$(kubectl_cmd --namespace "$namespace" get pvc "$pvc" -o jsonpath='{.status.phase}')" = Bound
test "$(kubectl_cmd --namespace "$namespace" get pvc "$pvc" -o jsonpath='{.spec.storageClassName}')" = longhorn
volume_name=$(kubectl_cmd --namespace "$namespace" get pvc "$pvc" -o jsonpath='{.spec.volumeName}')
original_uid=$(kubectl_cmd --namespace "$namespace" get pod postgresql-0 -o jsonpath='{.metadata.uid}')
kubectl_cmd --namespace "$namespace" delete pod postgresql-0 --wait=false >/dev/null
kubectl_cmd --namespace "$namespace" wait --for=delete pod/postgresql-0 --timeout=3m
kubectl_cmd --namespace "$namespace" rollout status statefulset/postgresql --timeout=10m
test "$(kubectl_cmd --namespace "$namespace" get pod postgresql-0 -o jsonpath='{.metadata.uid}')" != "$original_uid"
recovered=false
for attempt in $(seq 1 60); do
    if test "$(client_exec psql -At -c "select payload from ${validation_table} where id = '$payload'" 2>/dev/null || true)" = "$payload"; then
        recovered=true
        break
    fi
    sleep 2
done
test "$recovered" = true

for attempt in $(seq 1 120); do
    kubectl_cmd --namespace "$longhorn_namespace" get volumes.longhorn.io "$volume_name" \
        -o json >"$temporary_directory/volume.json"
    kubectl_cmd --namespace "$longhorn_namespace" get replicas.longhorn.io \
        -o json >"$temporary_directory/replicas.json"
    if uv run python -c \
        'import json,pathlib,sys; v=json.loads(pathlib.Path(sys.argv[1]).read_text()); rs=json.loads(pathlib.Path(sys.argv[2]).read_text())["items"]; selected=[r for r in rs if r["spec"]["volumeName"]==v["metadata"]["name"]]; assert v["status"]["robustness"]=="healthy"; assert len(selected)==3; assert {r["spec"]["nodeID"] for r in selected}=={"worker-1","worker-2","worker-3"}; assert all(r["status"]["currentState"]=="running" for r in selected)' \
        "$temporary_directory/volume.json" "$temporary_directory/replicas.json"; then
        break
    fi
    sleep 2
done
test "$attempt" -lt 120

client_exec psql -v ON_ERROR_STOP=1 -c "drop table ${validation_table}" >/dev/null
remove_validation_resources
test "$(kubectl_cmd --namespace "$namespace" get statefulset postgresql -o jsonpath='{.status.readyReplicas}')" = 1
test "$(kubectl_cmd get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')" = 6
! kubectl_cmd get namespace "$validation_namespace" >/dev/null 2>&1
! kubectl_cmd get namespace "$denied_namespace" >/dev/null 2>&1

trap - EXIT HUP INT TERM
rm -rf "$temporary_directory"+echo "PostgreSQL ${version} validation passed: TLS, policy denial, persistence and pod recovery are healthy."
rm -rf "$temporary_directory"
echo "PostgreSQL ${version} validation passed: TLS, policy denial, persistence and pod recovery are healthy."
