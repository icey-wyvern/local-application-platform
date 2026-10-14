#!/usr/bin/env bash
set -euo pipefail

kubectl_bin="${1:-${HOME}/.local/bin/kubectl}"
kubeconfig="${2:-${HOME}/.kube/lap-admin.conf}"
manifest_directory="${3:-cluster/platform-controller}"
registry_password_file="${4:-$HOME/.local/share/local-application-platform/secrets/build-host/registry-puller-password}"
lab_root_ca="${5:-.generated/pki/lab-root-ca.crt}"

[[ -s "$registry_password_file" ]] || {
  printf 'registry pull credential is missing: %s\n' "$registry_password_file" >&2
  exit 1
}
[[ -s "$lab_root_ca" ]] || {
  printf 'lab root CA is missing: %s\n' "$lab_root_ca" >&2
  exit 1
}

"$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
  --field-manager=lap-kustomize -f "$manifest_directory/namespace.yaml" >/dev/null

docker_config="$(mktemp)"
trap 'rm -f -- "$docker_config"' EXIT
umask 077
registry_password="$(<"$registry_password_file")"
registry_auth="$(printf '%s' "lap-registry-puller:$registry_password" | base64 -w0)"
printf '{"auths":{"registry.lab.test:5000":{"auth":"%s"}}}\n' "$registry_auth" >"$docker_config"
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  create secret generic platform-controller-registry \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$docker_config" --dry-run=client -o yaml \
  | "$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
      --field-manager=lap-secrets -f - >/dev/null
unset registry_password registry_auth

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  create configmap platform-controller-lab-root-ca \
  --from-file=lab-root-ca.crt="$lab_root_ca" --dry-run=client -o yaml \
  | "$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
      --field-manager=lap-pki -f - >/dev/null

"$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
  --field-manager=lap-kustomize --kustomize "$manifest_directory" >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  rollout status deployment/platform-controller --timeout=5m
