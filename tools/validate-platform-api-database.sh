#!/bin/sh
set -eu

kubectl=${1:?kubectl path is required}
kubeconfig=${2:?kubeconfig is required}
secret_directory=${3:?secret directory is required}
root_ca=${4:?root CA path is required}
database="api_foundation_$(date +%s)_$$"
forward_pid=

cleanup() {
    if [ -n "$forward_pid" ]; then
        kill "$forward_pid" 2>/dev/null || true
        wait "$forward_pid" 2>/dev/null || true
    fi
    "$kubectl" --kubeconfig "$kubeconfig" --namespace platform-database \
        exec statefulset/postgresql -- psql -U postgres -d postgres \
        -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$database\" WITH (FORCE)" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

"$kubectl" --kubeconfig "$kubeconfig" --namespace platform-database \
    exec statefulset/postgresql -- psql -U postgres -d postgres \
    -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$database\" OWNER platform_api" >/dev/null

"$kubectl" --kubeconfig "$kubeconfig" --namespace platform-database \
    port-forward service/postgresql 15432:5432 >/tmp/lap-platform-api-postgresql-forward.log 2>&1 &
forward_pid=$!
attempts=0
while ! nc -z 127.0.0.1 15432 >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 30 ] || {
        cat /tmp/lap-platform-api-postgresql-forward.log >&2
        exit 1
    }
    sleep 1
done

export POSTGRESQL_HOST=postgresql.platform-database.svc.cluster.local
export POSTGRESQL_HOST_ADDRESS=127.0.0.1
export POSTGRESQL_PORT=15432
export POSTGRESQL_DATABASE=$database
export POSTGRESQL_APPLICATION_USER=platform_api
POSTGRESQL_PASSWORD=$(cat "$secret_directory/postgresql-application-password")
export POSTGRESQL_PASSWORD
export POSTGRESQL_SSL_ROOT_CERT=$root_ca
export LAP_RUN_DATABASE_TESTS=1
export UV_PROJECT_ENVIRONMENT=.venv-linux-api

uv run --all-packages pytest platform/api/tests -m integration -q
unset POSTGRESQL_PASSWORD
echo 'Platform API PostgreSQL TLS, persistence, concurrency, and migration validation passed.'
