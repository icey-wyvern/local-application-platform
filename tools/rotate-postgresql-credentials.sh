#!/bin/sh
set -eu

if [ "$#" -ne 8 ]; then
    echo "usage: $0 KUBECTL KUBECONFIG NAMESPACE DATABASE APP_USER ADMIN_FILE APP_FILE POD" >&2
    exit 2
fi

kubectl_binary=$1
kubeconfig=$2
namespace=$3
database=$4
application_user=$5
administrative_file=$6
application_file=$7
pod=$8
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
umask 077

openssl rand -hex 32 | tr -d '\n' >"$temporary_directory/administrative"
openssl rand -hex 32 | tr -d '\n' >"$temporary_directory/application"
administrative_password=$(cat "$temporary_directory/administrative")
application_password=$(cat "$temporary_directory/application")

{
    printf '\\set administrative_password %s\n' "$administrative_password"
    printf '\\set application_password %s\n' "$application_password"
    printf "ALTER ROLE postgres PASSWORD :'administrative_password';\n"
    printf "ALTER ROLE %s PASSWORD :'application_password';\n" "$application_user"
} | "$kubectl_binary" --kubeconfig "$kubeconfig" --namespace "$namespace" \
    exec -i "$pod" -c postgresql -- psql --quiet --set=ON_ERROR_STOP=1 \
    --username postgres --dbname "$database"

install -m 600 "$temporary_directory/administrative" "$administrative_file"
install -m 600 "$temporary_directory/application" "$application_file"

"$kubectl_binary" --kubeconfig "$kubeconfig" --namespace "$namespace" \
    create secret generic postgresql-administrative \
    --from-file=password="$administrative_file" --dry-run=client -o yaml |
    "$kubectl_binary" --kubeconfig "$kubeconfig" apply --server-side \
        --field-manager=lap-secret-bootstrap -f - >/dev/null
"$kubectl_binary" --kubeconfig "$kubeconfig" --namespace "$namespace" \
    create secret generic postgresql-application \
    --from-literal=database="$database" --from-literal=username="$application_user" \
    --from-file=password="$application_file" --dry-run=client -o yaml |
    "$kubectl_binary" --kubeconfig "$kubeconfig" apply --server-side \
        --field-manager=lap-secret-bootstrap -f - >/dev/null

echo "PostgreSQL administrative and application credentials were rotated without disclosure."
