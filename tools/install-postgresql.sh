#!/bin/sh
set -eu

if [ "$#" -ne 9 ]; then
    echo "usage: $0 KUBECTL KUBECONFIG MANIFESTS NAMESPACE DATABASE APP_USER VERSION ADMIN_FILE APP_FILE" >&2
    exit 2
fi

kubectl_binary=$1
kubeconfig=$2
manifests=$3
namespace=$4
database=$5
application_user=$6
version=$7
administrative_file=$8
application_file=$9

generate_once() {
    destination=$1
    mkdir -p "$(dirname "$destination")"
    chmod 700 "$(dirname "$destination")"
    if [ -e "$destination" ]; then
        test -s "$destination" || {
            echo "refusing to replace empty existing credential file: $destination" >&2
            exit 1
        }
        normalized=$(tr -d '\r\n' <"$destination")
        case "$normalized" in
            *[!0-9a-f]* | "")
                echo "existing credential file is not a 64-character hexadecimal value: $destination" >&2
                exit 1
                ;;
        esac
        test "${#normalized}" -eq 64 || {
            echo "existing credential file is not a 64-character hexadecimal value: $destination" >&2
            exit 1
        }
        printf '%s' "$normalized" >"$destination"
        chmod 600 "$destination"
        return
    fi
    umask 077
    openssl rand -hex 32 | tr -d '\n' >"$destination"
    chmod 600 "$destination"
}

kubectl_cmd() {
    "$kubectl_binary" --kubeconfig "$kubeconfig" "$@"
}

generate_once "$administrative_file"
generate_once "$application_file"

kubectl_cmd apply --server-side --field-manager=lap-kustomize \
    -f "$manifests/namespace.yaml" >/dev/null

kubectl_cmd --namespace "$namespace" create secret generic postgresql-administrative \
    --from-file=password="$administrative_file" --dry-run=client -o yaml |
    kubectl_cmd apply --server-side --field-manager=lap-secret-bootstrap -f - >/dev/null
kubectl_cmd --namespace "$namespace" create secret generic postgresql-application \
    --from-literal=database="$database" \
    --from-literal=username="$application_user" \
    --from-file=password="$application_file" --dry-run=client -o yaml |
    kubectl_cmd apply --server-side --field-manager=lap-secret-bootstrap -f - >/dev/null

kubectl_cmd apply --server-side --field-manager=lap-kustomize \
    --kustomize "$manifests" >/dev/null
kubectl_cmd --namespace "$namespace" wait \
    --for=condition=Ready certificate/postgresql-server --timeout=5m
kubectl_cmd --namespace "$namespace" rollout status statefulset/postgresql --timeout=10m
kubectl_cmd --namespace "$namespace" wait \
    --for=condition=Ready pod/postgresql-0 --timeout=5m

installed_version=$(kubectl_cmd --namespace "$namespace" exec postgresql-0 -- \
    postgres --version | awk '{print $3}')
test "$installed_version" = "$version"
echo "PostgreSQL ${version} is installed and ready; credential files were preserved or created once."
