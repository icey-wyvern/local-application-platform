#!/bin/sh
set -eu

if [ "$#" -ne 9 ]; then
    echo "usage: $0 KUBECTL KUBECONFIG MANIFESTS NAMESPACE DATABASE APP_USER ADMIN_FILE APP_FILE VERSION" >&2
    exit 2
fi

kubectl_binary=$1
kubeconfig=$2
manifests=$3
namespace=$4
database=$5
application_user=$6
administrative_file=$7
application_file=$8
version=$9

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

pod_uid_before=$(kubectl_cmd --namespace "$namespace" get pod postgresql-0 -o jsonpath='{.metadata.uid}')
pvc_uid_before=$(kubectl_cmd --namespace "$namespace" get pvc data-postgresql-0 -o jsonpath='{.metadata.uid}')
credentials_before=$(cksum "$administrative_file" "$application_file")

sh tools/install-postgresql.sh "$kubectl_binary" "$kubeconfig" "$manifests" \
    "$namespace" "$database" "$application_user" "$version" \
    "$administrative_file" "$application_file" >/dev/null

test "$pod_uid_before" = "$(kubectl_cmd --namespace "$namespace" get pod postgresql-0 -o jsonpath='{.metadata.uid}')"
test "$pvc_uid_before" = "$(kubectl_cmd --namespace "$namespace" get pvc data-postgresql-0 -o jsonpath='{.metadata.uid}')"
test "$credentials_before" = "$(cksum "$administrative_file" "$application_file")"
test "$(kubectl_cmd --namespace "$namespace" exec postgresql-0 -c postgresql -- \
    psql -AtU postgres -d "$database" -c "select count(*) from pg_roles where rolname='$application_user'")" = 1

echo "PostgreSQL installation is idempotent: pod, PVC, credentials and role are unchanged."
