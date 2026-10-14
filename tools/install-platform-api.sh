#!/usr/bin/env bash
set -euo pipefail

kubectl_bin="${1:-${HOME}/.local/bin/kubectl}"
kubeconfig="${2:-${HOME}/.kube/lap-admin.conf}"
manifest_directory="${3:-cluster/platform-api}"
secret_directory="${4:-$HOME/.local/share/local-application-platform/secrets}"
pki_directory="${5:-.generated/pki}"
build_host_secret_directory="${6:-$secret_directory/build-host}"

token_file="$secret_directory/platform-api-token"
token_hash_file="$secret_directory/platform-api-token-sha256"
postgresql_password_file="$secret_directory/postgresql-application-password"
registry_password_file="$build_host_secret_directory/registry-puller-password"
client_certificate="$pki_directory/platform-api-build-agent-client.crt"
client_key="$pki_directory/platform-api-build-agent-client.key"
root_ca="$pki_directory/lab-root-ca.crt"

umask 077
install -d -m 0700 "$secret_directory"
if [[ ! -e "$token_file" && ! -e "$token_hash_file" ]]; then
  openssl rand -base64 32 >"$token_file"
  token_hash="$(tr -d '\n' <"$token_file" | sha256sum | cut -d' ' -f1)"
  printf '%s' "$token_hash" >"$token_hash_file"
  unset token_hash
elif [[ ! -s "$token_file" || ! -s "$token_hash_file" ]]; then
  printf 'platform API token state is incomplete; repair it explicitly\n' >&2
  exit 1
fi
chmod 0600 "$token_file" "$token_hash_file"

for required in "$postgresql_password_file" "$registry_password_file" \
  "$client_certificate" "$client_key" "$root_ca"; do
  [[ -s "$required" ]] || { printf 'required input is missing: %s\n' "$required" >&2; exit 1; }
done

expected_hash="$(tr -d '\n' <"$token_file" | sha256sum | cut -d' ' -f1)"
actual_hash="$(tr -d '\n' <"$token_hash_file")"
[[ "$expected_hash" = "$actual_hash" ]] || {
  printf 'platform API token and hash do not match\n' >&2
  exit 1
}
printf '%s' "$actual_hash" >"$token_hash_file"

"$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
  --field-manager=lap-kustomize -f "$manifest_directory/namespace.yaml" >/dev/null

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  create configmap platform-api-lab-root-ca \
  --from-file=lab-root-ca.crt="$root_ca" --dry-run=client -o yaml \
  | "$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
      --field-manager=lap-secrets -f - >/dev/null

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  create secret generic platform-api \
  --from-file=token-sha256="$token_hash_file" \
  --from-file=postgresql-password="$postgresql_password_file" \
  --dry-run=client -o yaml \
  | "$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
      --field-manager=lap-secrets -f - >/dev/null

"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  create secret generic platform-api-build-agent-mtls \
  --from-file=tls.crt="$client_certificate" \
  --from-file=tls.key="$client_key" \
  --from-file=ca.crt="$root_ca" --dry-run=client -o yaml \
  | "$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
      --field-manager=lap-secrets -f - >/dev/null

docker_config="$(mktemp)"
trap 'rm -f -- "$docker_config"' EXIT
registry_password="$(<"$registry_password_file")"
registry_auth="$(printf '%s' "lap-registry-puller:$registry_password" | base64 -w0)"
printf '{"auths":{"registry.lab.test:5000":{"auth":"%s"}}}\n' "$registry_auth" >"$docker_config"
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  create secret generic platform-api-registry \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$docker_config" --dry-run=client -o yaml \
  | "$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
      --field-manager=lap-secrets -f - >/dev/null
unset registry_password registry_auth

"$kubectl_bin" --kubeconfig "$kubeconfig" apply --server-side \
  --field-manager=lap-kustomize --kustomize "$manifest_directory" >/dev/null
"$kubectl_bin" --kubeconfig "$kubeconfig" --namespace platform-system \
  rollout status deployment/platform-api --timeout=5m
