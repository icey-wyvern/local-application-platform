#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 VERSION DESTINATION" >&2
    exit 2
fi

version=$1
destination=$2
archive="helm-v${version}-linux-amd64.tar.gz"
base_url="https://get.helm.sh"

if [ -x "$destination" ] && "$destination" version --short | grep -q "^v${version}+"; then
    exit 0
fi

temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --output "$temporary_directory/$archive" "$base_url/$archive"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --output "$temporary_directory/$archive.sha256sum" "$base_url/$archive.sha256sum"

(
    cd "$temporary_directory"
    sha256sum --check "$archive.sha256sum"
    tar --extract --gzip --file "$archive"
)

mkdir -p "$(dirname "$destination")"
install -m 0755 "$temporary_directory/linux-amd64/helm" "$destination"
