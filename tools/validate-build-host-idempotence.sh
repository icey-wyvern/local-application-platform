#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
    echo "usage: $0 SECRET_DIR PKI_DIR ANSIBLE_VERSION SSH_KEY" >&2
    exit 2
fi

secret_dir=$1
pki_dir=$2
ansible_version=$3
ssh_key=$4
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

output=$(cd "$repo_root/infrastructure/ansible" && \
    BUILD_HOST_SECRET_DIR="$secret_dir" \
    BUILD_HOST_PKI_DIR="$pki_dir" \
    ANSIBLE_CONFIG="$repo_root/infrastructure/ansible/ansible.cfg" \
    uv tool run --from "ansible-core==$ansible_version" ansible-playbook \
        playbooks/build-host.yaml --private-key "$ssh_key")
printf '%s\n' "$output"

recap=$(printf '%s\n' "$output" | grep -E '^build-1[[:space:]]*:')
printf '%s\n' "$recap" | grep -Eq 'changed=0([[:space:]]|$)'
printf '%s\n' "$recap" | grep -Eq 'failed=0([[:space:]]|$)'
printf '%s\n' "$recap" | grep -Eq 'unreachable=0([[:space:]]|$)'
echo 'Build-host Ansible reconciliation is idempotent (changed=0).'

