import uvicorn

from lap_build_agent.app import create_app
from lap_build_agent.config import Settings
from lap_build_agent.logging import configure_logging
from lap_build_agent.pipeline import cleanup_stale_workspaces
from lap_build_agent.telemetry import configure_telemetry


def run() -> None:
    configure_logging()
    settings = Settings.from_environment()
    cleanup_stale_workspaces(settings.state_path)
    configure_telemetry(settings)
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        access_log=False,
        log_config=None,
        server_header=False,
        ssl_certfile="/etc/lap-build-agent/tls/server.crt",
        ssl_keyfile="/etc/lap-build-agent/tls/server.key",
        ssl_ca_certs="/etc/lap-build-agent/tls/client-ca.crt",
        ssl_cert_reqs=2,
    )


if __name__ == "__main__":
    run()
