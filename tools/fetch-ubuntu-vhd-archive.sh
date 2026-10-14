#!/usr/bin/env sh
set -eu

base_url="https://cloud-images.ubuntu.com/releases/noble/release-20260826"
archive_name="ubuntu-24.04-server-cloudimg-amd64-azure.vhd.tar.gz"
work_dir="${1:-.local-tools/ubuntu-image}"
sums_path="${work_dir}/SHA256SUMS"
archive_path="${work_dir}/${archive_name}"

mkdir -p "${work_dir}"

curl -fL "${base_url}/SHA256SUMS" -o "${sums_path}"
curl -fL --continue-at - "${base_url}/${archive_name}" -o "${archive_path}"

expected_checksum="$(awk -v file="${archive_name}" '$2 == "*" file || $2 == file { print $1 }' "${sums_path}")"
if [ -z "${expected_checksum}" ]; then
  printf '%s\n' "No SHA256SUMS entry found for ${archive_name}" >&2
  exit 1
fi

actual_checksum="$(sha256sum "${archive_path}" | awk '{ print $1 }')"
if [ "${actual_checksum}" != "${expected_checksum}" ]; then
  printf '%s\n' "Checksum mismatch for ${archive_name}" >&2
  printf '%s\n' "expected: ${expected_checksum}" >&2
  printf '%s\n' "actual:   ${actual_checksum}" >&2
  exit 1
fi

tar -tzf "${archive_path}"
printf '%s\n' "Verified ${archive_name} (${actual_checksum})"
