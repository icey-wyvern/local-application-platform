import uvicorn

from lap_platform_api.app import create_app
from lap_platform_api.config import ApiSettings
from lap_platform_api.logging import configure_logging


def run() -> None:
    settings = ApiSettings.from_environment()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        access_log=False,
        log_config=None,
        server_header=False,
    )


if __name__ == "__main__":
    run()
