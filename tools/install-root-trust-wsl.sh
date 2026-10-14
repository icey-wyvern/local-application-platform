#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 ROOT_CERTIFICATE" >&2
    exit 2
fi

source_certificate=$1
destination=/usr/local/share/ca-certificates/lap-root-ca.crt
if [ "$(id -u)" -ne 0 ]; then
    echo "Run this trust-store installer as WSL root." >&2
    exit 1
fi
openssl x509 -in "$source_certificate" -noout -checkend 2592000

if test -f "$destination" && cmp --silent "$source_certificate" "$destination"; then
    echo "WSL already trusts the lab root CA."
else
    install -o root -g root -m 0644 "$source_certificate" "$destination"
    update-ca-certificates >/dev/null
    echo "Installed the lab root CA in the WSL trust store."
fi
