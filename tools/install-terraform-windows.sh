#!/usr/bin/env sh
set -eu

version="${1:?usage: install-terraform-windows.sh <version> <install-dir>}"
install_dir="${2:?usage: install-terraform-windows.sh <version> <install-dir>}"

zip_name="terraform_${version}_windows_amd64.zip"
sums_name="terraform_${version}_SHA256SUMS"
base_url="https://releases.hashicorp.com/terraform/${version}"
downloads_dir=".local-tools/tmp"
zip_path="${downloads_dir}/${zip_name}"
sums_path="${downloads_dir}/${sums_name}"
terraform_exe="${install_dir}/terraform.exe"

if [ -x "${terraform_exe}" ] && "${terraform_exe}" version -json >/dev/null 2>&1; then
  installed_version="$("${terraform_exe}" version -json | sed -n 's/.*"terraform_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  if [ "${installed_version}" = "${version}" ]; then
    printf '%s\n' "Terraform ${version} already installed at ${terraform_exe}"
    exit 0
  fi
fi

mkdir -p "${downloads_dir}" "${install_dir}"

curl -fsSL "${base_url}/${zip_name}" -o "${zip_path}"
curl -fsSL "${base_url}/${sums_name}" -o "${sums_path}"

expected_checksum="$(awk -v file="${zip_name}" '$2 == file { print $1 }' "${sums_path}")"
if [ -z "${expected_checksum}" ]; then
  printf '%s\n' "No checksum for ${zip_name} found in ${sums_path}" >&2
  exit 1
fi

actual_checksum="$(sha256sum "${zip_path}" | awk '{ print $1 }')"
if [ "${actual_checksum}" != "${expected_checksum}" ]; then
  printf '%s\n' "Checksum mismatch for ${zip_name}" >&2
  printf '%s\n' "expected: ${expected_checksum}" >&2
  printf '%s\n' "actual:   ${actual_checksum}" >&2
  exit 1
fi

tmp_extract="${downloads_dir}/terraform-${version}-extract"
rm -rf "${tmp_extract}"
mkdir -p "${tmp_extract}"
unzip -q -o "${zip_path}" -d "${tmp_extract}"

mv "${tmp_extract}/terraform.exe" "${terraform_exe}"
if [ -f "${tmp_extract}/LICENSE.txt" ]; then
  mv "${tmp_extract}/LICENSE.txt" "${install_dir}/LICENSE.txt"
fi
rm -rf "${tmp_extract}"

printf '%s\n' "Installed Terraform ${version} at ${terraform_exe}"
