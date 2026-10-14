import ipaddress
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lap_build_agent.errors import BuildError
from lap_build_agent.models import ApplicationMetadata

PLATFORM_YAML_MAX_BYTES = 16 * 1024
REDIRECT_LIMIT = 5
AddressInfo = tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]
Resolver = Callable[[str, int], list[AddressInfo]]


def _resolve(host: str, port: int) -> list[AddressInfo]:
    return cast("list[AddressInfo]", socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))


class _PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")]
    port: Annotated[int, Field(ge=1024, le=65535)]
    healthPath: Annotated[str, Field(min_length=1, max_length=256)]

    @field_validator("healthPath")
    @classmethod
    def validate_health_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not value.startswith("/") or parsed.scheme or parsed.netloc:
            raise ValueError("must be an absolute path without a scheme or hostname")
        if parsed.query or parsed.fragment:
            raise ValueError("must not contain a query string or fragment")
        if value.startswith("//"):
            raise ValueError("must not be a network-path reference")
        return value


def validate_platform_yaml(path: Path) -> ApplicationMetadata:
    if path.is_symlink():
        raise BuildError(
            "platform_config_invalid",
            "platform.yaml must be a regular file, not a symbolic link",
            stage="validate",
        )
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise BuildError(
            "platform_config_missing", "repository does not contain platform.yaml", stage="validate"
        ) from error
    if len(raw) > PLATFORM_YAML_MAX_BYTES:
        raise BuildError(
            "platform_config_too_large",
            "platform.yaml exceeds the 16384-byte limit",
            stage="validate",
        )
    try:
        text = raw.decode("utf-8")
        if any(isinstance(token, yaml.AliasToken) for token in yaml.scan(text)):
            raise yaml.YAMLError("YAML aliases are not permitted")
        loaded = yaml.safe_load(text)
        config = _PlatformConfig.model_validate(loaded)
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError):
        raise BuildError(
            "platform_config_invalid", "platform.yaml is invalid", stage="validate"
        ) from None
    return ApplicationMetadata.model_validate(config.model_dump())


def _validate_public_host(host: str, port: int, resolver: Resolver) -> None:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith((".localhost", ".local", ".test")):
        raise BuildError(
            "repository_destination_forbidden", "repository host is not public", stage="clone"
        )
    try:
        answers = resolver(host, port)
    except socket.gaierror as error:
        raise BuildError(
            "repository_dns_failed", "repository hostname could not be resolved", stage="clone"
        ) from error
    if not answers:
        raise BuildError(
            "repository_dns_failed", "repository hostname has no addresses", stage="clone"
        )
    for answer in answers:
        address = ipaddress.ip_address(str(answer[4][0]))
        if not address.is_global:
            raise BuildError(
                "repository_destination_forbidden",
                "repository hostname resolves to a non-public address",
                stage="clone",
            )


def validate_repository_url(
    repository_url: str,
    *,
    resolver: Resolver = _resolve,
) -> str:
    parsed = urlsplit(repository_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise BuildError("repository_url_invalid", "repository URL must use HTTPS", stage="clone")
    if parsed.username is not None or parsed.password is not None:
        raise BuildError(
            "repository_credentials_forbidden",
            "repository URL must not contain credentials",
            stage="clone",
        )
    if parsed.query or parsed.fragment:
        raise BuildError(
            "repository_url_invalid",
            "repository URL must not contain query or fragment",
            stage="clone",
        )
    if parsed.port not in (None, 443):
        raise BuildError(
            "repository_url_invalid",
            "repository URL must use the standard HTTPS port",
            stage="clone",
        )
    _validate_public_host(parsed.hostname, 443, resolver)
    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def resolve_public_git_url(
    repository_url: str,
    *,
    resolver: Resolver = _resolve,
    transport: httpx.BaseTransport | None = None,
) -> str:
    current = validate_repository_url(repository_url, resolver=resolver)
    endpoint = f"{current}/info/refs?service=git-upload-pack"
    with httpx.Client(follow_redirects=False, timeout=10, transport=transport) as client:
        for _ in range(REDIRECT_LIMIT + 1):
            with client.stream(
                "GET",
                endpoint,
                headers={"Accept": "application/x-git-upload-pack-advertisement"},
            ) as response:
                status_code = response.status_code
                headers = response.headers
            if status_code in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    break
                endpoint = urljoin(endpoint, location)
                suffix = "/info/refs?service=git-upload-pack"
                candidate = endpoint[: -len(suffix)] if endpoint.endswith(suffix) else endpoint
                current = validate_repository_url(candidate, resolver=resolver)
                endpoint = f"{current}/info/refs?service=git-upload-pack"
                continue
            if status_code != 200:
                raise BuildError(
                    "repository_not_public",
                    f"public Git discovery failed with HTTP {status_code}",
                    stage="clone",
                )
            content_type = headers.get("content-type", "").split(";", 1)[0]
            if content_type != "application/x-git-upload-pack-advertisement":
                raise BuildError(
                    "repository_not_git",
                    "URL did not return a public Git repository",
                    stage="clone",
                )
            return current
    raise BuildError(
        "repository_redirect_invalid", "repository redirected too many times", stage="clone"
    )
