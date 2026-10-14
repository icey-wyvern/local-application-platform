# Build agent and secure build pipeline

The platform runs `platform-build-agent` on `build-1` as the dedicated
`lap-build-agent` system identity. The only network listener is
`https://build-agent.lab.test:8443`; nftables admits it from the three control
planes, worker nodes hosting the platform API, and the routed administration
address. It is not exposed through Envoy Gateway. Uvicorn requires a lab-CA-signed client certificate during the TLS
handshake, so application routing cannot reach an unauthenticated build
operation.

## API contract

`POST /v1/builds` is synchronous and accepts:

```json
{
  "buildId": "36b34451-e55a-4d06-91f4-13492a607b9f",
  "repositoryUrl": "https://github.com/example/application.git",
  "revision": {"type": "branch", "value": "main"}
}
```

`revision.type` is `branch`, `tag`, or `commit`; a commit value must be a full
40-character hash. Success returns the supplied build ID, resolved immutable
commit, digest-qualified registry reference, registry digest, validated
application metadata, and vulnerability counts. Failures use an `error` object
with stable `code`, `message`, optional `stage`, and `retryable` fields.

The process retains at most 128 completed results in memory so an identical
retry of a build ID does not rebuild, and rejects reuse with different input.
This cache is deliberately ephemeral. The platform API and PostgreSQL own
queuing, durable status, history, and recovery decisions.
An agent restart loses this cache and any active request; it never silently
creates a new durable operation.

`GET /healthz` and `GET /metrics` are operational endpoints on the same mTLS
listener. Interactive API documentation is disabled.

## Git security model

Only public HTTPS repositories on standard port 443 are accepted. User-info,
queries, fragments, SSH, `git://`, `file://`, local paths, `.test`, `.local`,
localhost, and any DNS answer that is not globally routable are rejected.
Git smart-HTTP discovery is performed first, each redirect is resolved and
validated again, and Git redirects are then disabled for the clone. Git is run
without prompts, credential helpers, system/global configuration, SSH, or any
protocol other than HTTPS. The requested ref is shallow-fetched where the
server supports it, resolved through `FETCH_HEAD^{commit}`, and checked out
detached. Repositories containing `.gitmodules` are rejected.

The checks limit DNS rebinding but cannot make an application-layer DNS check
atomic with Git's later connection. The service cgroup additionally denies all
private, loopback, link-local, reserved, multicast, lab, Pod, and Service
ranges, with narrow exceptions only for its local registry, the trusted API
peers, the host's exact `127.0.0.53` DNS stub, and the telemetry receiver. The local registry still requires separate
credentials and the build-agent endpoint still requires mTLS.

## `platform.yaml`

The file must be a regular UTF-8 YAML file at repository root and no larger
than 16 KiB. Safe parsing is used and aliases are rejected. Exactly these
properties are accepted:

| Property | Contract |
| --- | --- |
| `name` | Lowercase DNS-safe name, 1-48 characters |
| `port` | Integer TCP port, 1024-65535 |
| `healthPath` | Starts with `/`, at most 256 characters, and has no scheme, hostname, query, or fragment |

Unknown properties, type coercion, symbolic links, malformed YAML, missing
files, and invalid values fail at the validation stage. The build agent does
not create an `Application` custom resource or deploy the image.

## Exact artifact pipeline

The immutable sequence is:

`Git fetch -> commit resolution -> platform.yaml validation -> rootless BuildKit -> OCI archive -> Trivy JSON -> policy -> Skopeo copy -> registry digest verification`

BuildKit writes a unique per-request OCI archive and cannot reach the registry.
Trivy scans a lossless extraction of that exact archive. The archive hash is
checked before and after scanning. Only after policy acceptance does Skopeo
copy the original archive with `--preserve-digests`; a final registry inspect
must equal the source manifest digest. There is no rebuild between scan and
publication and registry TLS verification is never disabled.

Temporary source, OCI layout, archive, and report files live in a mode-0700
`TemporaryDirectory` and are removed on success, validation failure, tool
failure, policy rejection, or timeout. A 15-minute overall deadline applies.
Subprocess groups are killed on timeout, and systemd stops remaining cgroup
processes during restart. A non-blocking `flock` at
`/var/lib/lap-build-agent/build.lock` enforces one local build; a second build
receives HTTP 409 rather than creating an agent-side queue.

## Vulnerability policy

`LAP_VULNERABILITY_THRESHOLD` is authoritative and defaults to `CRITICAL`.
Every vulnerability at or above the configured severity blocks publication,
whether or not a fix is available. The scan does not use `--ignore-unfixed`.
Malformed/missing Trivy JSON, database errors, timeouts, and non-zero scanner
exit status are scanner failures, never clean scans. Rejected artifacts remain
only in the temporary workspace and are deleted without publication.

## Identity, secrets, and telemetry

The Windows-held lab root issues a 90-day `build-agent.lab.test` server leaf
with server EKU and a distinct `platform-api-build-agent-client` leaf with
client EKU. The generation script reuses valid matching keypairs and refuses
partial state. Private keys stay in gitignored `.generated/pki`; Ansible gives
the server key only to the build-agent group. The API client key is not
copied to `build-1`; it is stored in a Kubernetes Secret.

Logs are one-line JSON with `build_id`, `stage`, `result`, and stable error
code fields. OpenTelemetry spans cover clone, validate, build, scan, publish,
and verify. Stage/result counters and stage-duration histograms avoid commit,
digest, and request-ID labels. Logs, traces, and metrics are sent over the
narrow build-1-to-worker OTLP/HTTP NodePort to the existing Collector; the
Collector exports logs to Loki, traces to Tempo, and metrics for Prometheus.
The mTLS `/metrics` endpoint also exposes Prometheus-compatible metrics.

## Operations

Run `make build-agent-install` to reconcile the worker firewall/Collector
ingress and the build-host role. Run `make build-agent-check` for service,
mTLS, endpoint, and lab pipeline validation. Re-running provisioning preserves
the lab CA, leaf keypairs, registry credentials, BuildKit cache, and registry
content. Build-agent state is disposable except for the lock file, registry
auth file, and Trivy cache; none is authoritative application/build history.

For a full pipeline check, expose the read-only fixture produced by
`tools/serve-build-agent-fixtures.py` at a disposable public HTTPS Git URL and
set `BUILD_AGENT_TEST_REPOSITORY` to that URL when running
`make build-agent-check`. The fixture covers success, invalid configuration,
Dockerfile failure, and critical-vulnerability rejection. Remove the published
success manifest and stop the temporary public endpoint after capturing the
result; rejected fixtures must have no registry manifest to remove.

When the fixture is used through the platform API against its durable database,
pass `--name-suffix <unique-lowercase-value>` to the fixture server. This keeps
each disposable validation compatible with the permanent application-name and
repository identity contract; a previously soft-deleted name is intentionally
not reusable.

During a temporary registry outage the agent keeps its already built and
scanned OCI archive in the active build workspace and retries publication until
the configured build deadline. It checks the target digest before each copy, so
an earlier successful copy is reused. A deadline failure is terminal for that
Build ID; a later operator submission creates a new Build. The registry service
is a wanted systemd dependency, so stopping it does not stop the build agent.
