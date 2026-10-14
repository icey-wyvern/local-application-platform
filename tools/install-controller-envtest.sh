#!/bin/sh
set -eu

version=${1:?envtest version is required}
destination=${2:?destination directory is required}
expected_sha256=${3:?archive SHA-256 is required}
archive="envtest-v${version}-linux-amd64.tar.gz"
temporary_directory=$(mktemp -d)

cleanup() {
    rm -rf -- "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

if [ -x "$destination/etcd" ] && [ -x "$destination/kube-apiserver" ]; then
    exit 0
fi

curl --fail --location --silent --show-error \
    "https://github.com/kubernetes-sigs/controller-tools/releases/download/envtest-v${version}/${archive}" \
    --output "$temporary_directory/$archive"
printf '%s  %s\n' "$expected_sha256" "$temporary_directory/$archive" | sha256sum --check --status
rm -rf -- "$destination"
mkdir -p "$destination"
tar -xzf "$temporary_directory/$archive" -C "$temporary_directory"
find "$temporary_directory" -type f -perm -u+x -exec cp {} "$destination/" \;
