#!/bin/sh
set -eu

version=${1:?Gateway API version is required}
destination=${2:?destination directory is required}
expected_sha256=${3:?Gateway API manifest SHA-256 is required}
manifest="$destination/standard-install.yaml"
url="https://github.com/kubernetes-sigs/gateway-api/releases/download/v${version}/standard-install.yaml"

if [ -f "$manifest" ] && printf '%s  %s\n' "$expected_sha256" "$manifest" | sha256sum --check --status; then
    exit 0
fi

mkdir -p "$destination"
temporary=$(mktemp)
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
curl --fail --location --silent --show-error --output "$temporary" "$url"
printf '%s  %s\n' "$expected_sha256" "$temporary" | sha256sum --check --status
mv "$temporary" "$manifest"
trap - EXIT HUP INT TERM
