#!/bin/sh
set -eu

version=${1:?Go version is required}
destination=${2:?destination directory is required}
expected_sha256=${3:?archive SHA-256 is required}
archive="go${version}.linux-amd64.tar.gz"
temporary_directory=$(mktemp -d)

cleanup() {
    rm -rf -- "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

if [ -x "$destination/bin/go" ] && [ "$("$destination/bin/go" version)" = "go version go${version} linux/amd64" ]; then
    exit 0
fi

curl --fail --location --silent --show-error \
    "https://go.dev/dl/$archive" --output "$temporary_directory/$archive"
printf '%s  %s\n' "$expected_sha256" "$temporary_directory/$archive" | sha256sum --check --status
rm -rf -- "$destination"
mkdir -p "$(dirname "$destination")"
tar -xzf "$temporary_directory/$archive" -C "$temporary_directory"
mv "$temporary_directory/go" "$destination"
