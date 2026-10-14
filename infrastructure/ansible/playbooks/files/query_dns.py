#!/usr/bin/env python3
"""Query one IPv4 DNS record without installing diagnostic packages."""

import ipaddress
import socket
import struct
import sys


def encode_name(name: str) -> bytes:
    labels = (bytes([len(label)]) + label.encode("ascii") for label in name.split("."))
    return b"".join(labels) + b"\0"


def skip_name(message: bytes, offset: int) -> int:
    while True:
        length = message[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        offset += length


def query(server: str, name: str) -> set[str]:
    query_id = 0x4C41
    request = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    request += encode_name(name) + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.sendto(request, (server, 53))
        response = sock.recv(4096)

    response_id, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", response[:12])
    if response_id != query_id or flags & 0x000F or questions != 1:
        raise RuntimeError("invalid DNS response")

    offset = skip_name(response, 12) + 4
    addresses: set[str] = set()
    for _ in range(answers):
        offset = skip_name(response, offset)
        record_type, record_class, _, length = struct.unpack(
            "!HHIH", response[offset : offset + 10]
        )
        offset += 10
        data = response[offset : offset + length]
        offset += length
        if record_type == 1 and record_class == 1 and length == 4:
            addresses.add(str(ipaddress.ip_address(data)))
    return addresses


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: query_dns.py SERVER NAME EXPECTED", file=sys.stderr)
        return 2
    server, name, expected = sys.argv[1:]
    addresses = query(server, name)
    if addresses != {expected}:
        print(f"{name}: expected {expected}, received {sorted(addresses)}", file=sys.stderr)
        return 1
    print(f"{name} -> {expected} via {server}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
