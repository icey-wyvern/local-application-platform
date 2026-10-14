#!/usr/bin/env python3
"""Persist the authoritative lab DNS server in the WSL administration distro."""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import tempfile
from pathlib import Path

WSL_CONFIG = Path("/etc/wsl.conf")
RESOLV_CONFIG = Path("/etc/resolv.conf")


def with_wsl_network_setting(content: str) -> str:
    lines = content.splitlines(keepends=True)
    network_start = next(
        (index for index, line in enumerate(lines) if line.strip().lower() == "[network]"),
        None,
    )
    if network_start is None:
        return content.rstrip("\n") + "\n\n[network]\ngenerateResolvConf=false\n"
    network_end = next(
        (
            index
            for index in range(network_start + 1, len(lines))
            if lines[index].strip().startswith("[")
        ),
        len(lines),
    )
    settings = [
        index
        for index in range(network_start + 1, network_end)
        if lines[index].strip().split("=", 1)[0].strip().lower() == "generateresolvconf"
    ]
    if len(settings) > 1:
        raise ValueError("duplicate WSL resolver settings")
    if settings:
        lines[settings[0]] = "generateResolvConf=false\n"
    else:
        lines.insert(network_end, "generateResolvConf=false\n")
    return "".join(lines)


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def configure(server: str, hostname: str, expected_address: str) -> None:
    ipaddress.IPv4Address(server)
    ipaddress.IPv4Address(expected_address)
    if not hostname.endswith(".lab.test") or hostname.count(".") < 2:
        raise ValueError("API hostname must belong to the lab test domain")
    old_wsl = WSL_CONFIG.read_text() if WSL_CONFIG.exists() else ""
    new_wsl = with_wsl_network_setting(old_wsl)
    resolv = f"# Managed by local-application-platform startup\nnameserver {server}\n"
    if WSL_CONFIG.is_file() and old_wsl != new_wsl:
        atomic_write(WSL_CONFIG, new_wsl, WSL_CONFIG.stat().st_mode & 0o777)
    elif not WSL_CONFIG.exists():
        atomic_write(WSL_CONFIG, new_wsl)
    if RESOLV_CONFIG.is_symlink() or RESOLV_CONFIG.read_text() != resolv:
        atomic_write(RESOLV_CONFIG, resolv)
    addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)}
    if addresses != {expected_address}:
        raise RuntimeError("lab API hostname did not resolve to the configured Gateway address")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: configure-wsl-lab-dns.py SERVER HOSTNAME EXPECTED_ADDRESS")
    configure(*sys.argv[1:])
