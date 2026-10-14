#!/usr/bin/env bash
set -euo pipefail

build_host="${1:-10.20.0.51}"
ssh_key="${2:-${HOME}/.ssh/lap_lab_ed25519}"
image="${3:-registry.lab.test:5000/platform/controller-runtime-fixture:0.1.0}"
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
remote_directory=/var/tmp/lap-controller-runtime-fixture-0.1.0
archive="$remote_directory/runtime-fixture.oci.tar"
ssh_options=(-i "$ssh_key" -o BatchMode=yes -o StrictHostKeyChecking=yes "ubuntu@$build_host")

resolved="$(ssh "${ssh_options[@]}" readlink -m "$remote_directory")"
test "$resolved" = "$remote_directory"
cleanup() {
  ssh "${ssh_options[@]}" "sudo rm -rf -- '$remote_directory'" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
ssh "${ssh_options[@]}" \
  "sudo rm -rf -- '$remote_directory' && sudo install -d -o lap-buildkit -g lap-build -m 0750 '$remote_directory'"

tar -C "$repository_root" -czf - platform/controller/testdata/runtime-image/Dockerfile \
  | ssh "${ssh_options[@]}" \
      "sudo -u lap-buildkit tar -xzf - -C '$remote_directory' --strip-components=4"

ssh "${ssh_options[@]}" sudo -u lap-buildkit -- \
  buildctl --addr unix:///run/lap-buildkit/buildkitd.sock build \
  --frontend dockerfile.v0 \
  --local "context=$remote_directory" \
  --local "dockerfile=$remote_directory" \
  --opt filename=Dockerfile \
  --output "type=oci,dest=$archive,name=$image" >&2

ssh "${ssh_options[@]}" \
  "sudo chgrp lap-build '$archive' && sudo chmod 0640 '$archive' && sudo -u lap-build-agent skopeo copy --authfile /var/lib/lap-build-agent/registry-auth.json --preserve-digests 'oci-archive:$archive' 'docker://$image'" >&2

digest="$(ssh "${ssh_options[@]}" \
  "sudo -u lap-build-agent skopeo inspect --authfile /var/lib/lap-build-agent/registry-auth.json --format '{{.Digest}}' 'docker://$image'")"
printf '%s@%s\n' "${image%:*}" "$digest"
