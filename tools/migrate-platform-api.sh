#!/bin/sh
set -eu

kubectl=${1:?kubectl path is required}
kubeconfig=${2:?kubeconfig is required}
secret_directory=${3:?secret directory is required}
root_ca=${4:?root CA path is required}
local_port=${5:-15432}
forward_pid=

cleanup() {
    if [ -n "$forward_pid" ]; then
        kill "$forward_pid" 2>/dev/null || true
        wait "$forward_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT HUP INT TERM

"$kubectl" --kubeconfig "$kubeconfig" --namespace platform-database \
    port-forward service/postgresql "$local_port:5432" \
    >/tmp/lap-platform-api-migration-forward.log 2>&1 &
forward_pid=$!
attempts=0
while ! nc -z 127.0.0.1 "$local_port" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 30 ] || {
        cat /tmp/lap-platform-api-migration-forward.log >&2
        exit 1
    }
    sleep 1
done

export POSTGRESQL_HOST=postgresql.platform-database.svc.cluster.local
export POSTGRESQL_HOST_ADDRESS=127.0.0.1
export POSTGRESQL_PORT=$local_port
export POSTGRESQL_DATABASE=platform
export POSTGRESQL_APPLICATION_USER=platform_api
POSTGRESQL_PASSWORD=$(cat "$secret_directory/postgresql-application-password")
export POSTGRESQL_PASSWORD
export POSTGRESQL_SSL_ROOT_CERT=$root_ca
export UV_PROJECT_ENVIRONMENT=.venv-linux-api

uv_command=$(command -v uv || command -v "$HOME/.local/bin/uv")
"$uv_command" run --package platform-api python - <<'PY'
from alembic import command
from alembic.config import Config

from lap_platform_api.database import DatabaseSettings, create_database_engine

engine = create_database_engine(DatabaseSettings.from_environment())
try:
    with engine.connect() as connection:
        configuration = Config("platform/api/alembic.ini")
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "head")
        connection.commit()
        command.current(configuration)
        command.check(configuration)
finally:
    engine.dispose()
PY
unset POSTGRESQL_PASSWORD
echo 'Platform API database is at the current Alembic head.'
