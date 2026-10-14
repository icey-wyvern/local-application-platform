import socket
from pathlib import Path

import httpx
import pytest
from lap_build_agent.errors import BuildError
from lap_build_agent.validation import (
    AddressInfo,
    resolve_public_git_url,
    validate_platform_yaml,
    validate_repository_url,
)


def public_resolver(_host: str, port: int) -> list[AddressInfo]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/repo.git",
        "ssh://git@github.com/example/repo.git",
        "git://github.com/example/repo.git",
        "file:///tmp/repo",
        "https://user:password@github.com/example/repo.git",
        "https://github.com:8443/example/repo.git",
    ],
)
def test_rejects_unsafe_repository_urls(url: str) -> None:
    with pytest.raises(BuildError):
        validate_repository_url(url, resolver=public_resolver)


def test_rejects_private_and_lab_destinations() -> None:
    def private_resolver(_host: str, port: int) -> list[AddressInfo]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.0.51", port))]

    with pytest.raises(BuildError, match="non-public"):
        validate_repository_url("https://example.com/repo.git", resolver=private_resolver)
    with pytest.raises(BuildError, match="not public"):
        validate_repository_url("https://registry.lab.test/repo.git", resolver=public_resolver)


def test_redirect_destination_is_revalidated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://internal.test/repo.git"})

    with pytest.raises(BuildError, match="not public"):
        resolve_public_git_url(
            "https://example.com/repo.git",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        )


def test_public_git_discovery_returns_validated_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/info/refs")
        return httpx.Response(
            200, headers={"content-type": "application/x-git-upload-pack-advertisement"}
        )

    assert (
        resolve_public_git_url(
            "https://example.com/repo.git",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        )
        == "https://example.com/repo.git"
    )


def test_platform_yaml_contract(tmp_path: Path) -> None:
    path = tmp_path / "platform.yaml"
    path.write_text("name: example-api\nport: 8080\nhealthPath: /health\n")
    assert validate_platform_yaml(path).name == "example-api"


@pytest.mark.parametrize(
    "content",
    [
        "name: INVALID\nport: 8080\nhealthPath: /health\n",
        "name: example\nport: 80\nhealthPath: /health\n",
        "name: example\nport: 8080\nhealthPath: https://evil.test/x\n",
        "name: example\nport: 8080\nhealthPath: /health?q=x\n",
        "name: example\nport: 8080\nhealthPath: /health\nextra: true\n",
        "name: &name example\nport: 8080\nhealthPath: *name\n",
    ],
)
def test_platform_yaml_rejects_invalid_values(tmp_path: Path, content: str) -> None:
    path = tmp_path / "platform.yaml"
    path.write_text(content)
    with pytest.raises(BuildError) as raised:
        validate_platform_yaml(path)
    assert raised.value.code == "platform_config_invalid"


def test_platform_yaml_size_limit(tmp_path: Path) -> None:
    path = tmp_path / "platform.yaml"
    path.write_bytes(b"x" * (16 * 1024 + 1))
    with pytest.raises(BuildError) as raised:
        validate_platform_yaml(path)
    assert raised.value.code == "platform_config_too_large"


def test_invalid_platform_yaml_does_not_echo_sensitive_input(tmp_path: Path) -> None:
    marker = "sensitive-test-value-6d3d"
    path = tmp_path / "platform.yaml"
    path.write_text(f"name: example\nport: 8080\nhealthPath: /health\nsecret: {marker}\n")

    with pytest.raises(BuildError) as raised:
        validate_platform_yaml(path)

    assert raised.value.code == "platform_config_invalid"
    assert marker not in str(raised.value)
