# Platform API

The platform API is a FastAPI service in `platform/api`. It provides the HTTP,
security, persistence, build-agent dispatch, health, telemetry, durable public
request boundaries, and the operation scheduler core. It creates immutable
Releases from successful verified builds and writes the corresponding desired
`Application` custom resource. Runtime resources remain controller-owned.

## Service structure and endpoints

`lap_platform_api.app.create_app` is the application factory. Its lifespan owns
the SQLAlchemy engine, session factory, typed build-agent client, and
OpenTelemetry providers. `database_session` is the request-scoped SQLAlchemy
session dependency. Public routes use the persistence repositories; API code
does not recreate database identity, history, or concurrency rules.

The current endpoints are:

| Endpoint | Authentication | Meaning |
| --- | --- | --- |
| `GET /healthz` | anonymous | Process liveness only; it performs no dependency I/O. |
| `GET /readyz` | anonymous | Cached PostgreSQL and Kubernetes API checks. |
| `GET /metrics` | anonymous, internal network only | Prometheus text exposition. |
| `GET /api/v1/info` | bearer token | API name, namespace version, and service version. |
| `POST /api/v1/deployments` | bearer token and idempotency key | Record deploy intent and its pending Build. |
| `GET /api/v1/operations/{id}` | bearer token | Poll durable asynchronous status. |
| `GET /api/v1/applications` | bearer token | List non-deleted applications. |
| `GET /api/v1/applications/{name}` | bearer token | Read one non-deleted application. |
| `POST /api/v1/applications/{name}/rollbacks` | bearer token and idempotency key | Activate a previously healthy Release by ID. |
| `DELETE /api/v1/applications/{name}` | bearer token and idempotency key | Record asynchronous deletion intent. |
| `GET /api/v1/applications/{name}/builds` | bearer token | Read application build history. |
| `GET /api/v1/applications/{name}/releases` | bearer token | Read immutable release history. |
| `GET /api/v1/applications/{name}/deployments` | bearer token | Read deployment history. |
| `GET /api/v1/applications/{name}/operations` | bearer token | Read operation history. |
| `GET /api/v1/builds/{id}` | bearer token | Read one build. |
| `GET /api/v1/releases/{id}` | bearer token | Read one release. |
| `GET /api/v1/deployments/{id}` | bearer token | Read one deployment. |

Application creation follows the accepted first-deployment contract: the name
comes from validated `platform.yaml`, so `POST /api/v1/deployments` creates a
queued Operation and pending Build before an Application row exists. Build
dispatch associates or creates the Application only after the build agent has
validated that file and completed its pipeline successfully. Likewise,
`DELETE` records intent. Its scheduler handler deletes the Application custom
resource, waits for controller finalizer and runtime namespace cleanup, then
soft-deletes the database identity. Historical records remain.

History collections accept `offset` and `limit` (`1..100`) and are ordered
deterministically. Deleted applications are absent from name/list/history
collection routes, while their durable operations and other historical records
remain stored and addressable by identifier.

## Mutating requests and idempotency

Every mutation requires a canonical UUIDv4 `Idempotency-Key`. The API hashes a
canonical JSON representation containing the operation type and normalized
request content with SHA-256, then stores the hash on Operation. It never stores
the request body as an idempotency payload.

- The first valid request returns `202 Accepted`, an Operation resource, and a
  `Location` header pointing at its polling URL.
- The same key and canonical request returns that original Operation, including
  after an application has been soft-deleted.
- The same key with different canonical content returns `409` and
  `IDEMPOTENCY_CONFLICT`.
- A transaction-scoped PostgreSQL advisory lock for the key prevents the two
  relevant unique indexes from deadlocking during concurrent duplicate inserts,
  while the unique constraint remains authoritative.

Repository URLs are syntax-normalized for identity and hashing. Only HTTPS URLs
without embedded credentials, query strings, fragments, or non-standard ports
are accepted. Public destination/DNS validation remains the build-agent's
pre-clone responsibility. Branches and tags are stored as canonical full Git
references so later dispatch does not lose revision type.

## Operation scheduler

Each API process starts one scheduler through FastAPI lifespan management and
stops it before database resources and telemetry providers. The lease owner is
unique to the Pod and process: Pod name, process ID, and a random UUID. Both API
replicas run schedulers concurrently; PostgreSQL row locks and leases, rather
than in-process coordination, assign ownership.

The scheduler has exactly two execution slots per process. It claims only the
number of free slots in a short transaction using the persistence layer's
`FOR UPDATE SKIP LOCKED` query, commits, and then invokes a handler. Each claim
or expired-lease reclaim increments `attempts`. A lease lasts 45 seconds and is
renewed every 10 seconds while the handler is active. Completion, retry, and
terminal-failure updates require the same owner and a still-unexpired lease, so
a worker that loses ownership cannot overwrite its replacement's result.

Handlers return one of three explicit results:

- `succeeded` makes the Operation terminal and clears lease/failure metadata;
- `retry` records machine-readable failure metadata and atomically requeues the
  Operation;
- `failed` records failure metadata and completion time as a terminal result.

Unexpected handler exceptions are treated as retryable scheduler errors. A
failed lease renewal cancels the local handler and leaves state untouched for
the current/new owner. Graceful shutdown cancels active handlers and requeues
leases it still owns immediately. A hard Pod loss cannot perform that release;
another replica reclaims the row after lease expiry. Future external actions
must therefore be idempotent.

The deploy handler loads its associated Build, reconstructs the explicit branch,
tag, or commit revision, and calls the build agent with the durable Build UUID as
the build ID. The agent resolves Git, validates `platform.yaml`, builds and scans
the image, publishes it, and verifies the registry digest. A successful response
is persisted in one transaction: the Application is associated or created from
the validated name, the Build records the immutable commit and succeeds, and one
immutable Release records the digest-qualified image, port, and health path.
Application names and normalized repositories retain their permanent one-to-one
identity, including after soft deletion. A successful build does not by itself
advance `Application.current_release_id`; only final deployment acceptance does.

Build-agent request IDs are stable across retries. The agent returns an active
duplicate as retryable and caches completed results, while the database uniquely
constrains Release by Build. Thus graceful restart, lease expiry, or a response
lost after publication can safely retry without creating a second Release. Every
Build/Application/Release write locks and rechecks the running Operation's owner
and unexpired lease in the same transaction. A stale worker cannot commit after
lease loss. Delete and manual rollback use the same durable scheduler lease and
idempotency boundaries.

After Release persistence, the handler creates or resumes one durable Deployment
intent and writes the cluster-scoped `Application` spec. The mapping is exact:
Release ID, digest-qualified image reference, port, health path, and two replicas.
The API uses the Pod service-account token and cluster CA with normal API-server
TLS verification. Creates are idempotent; updates use a JSON Patch
`resourceVersion` test so competing writes fail atomically instead of silently
overwriting each other.

Before every external mutation the handler rechecks the PostgreSQL operation
lease. It also refuses to replace a CR that references an unknown Release or a
newer Release for that Application. A lost owner cannot persist observation or
operation success, while a replacement owner safely reuses the existing Release,
Deployment intent, and CR spec.

The handler uses one durable five-minute deadline, measured from the persisted
Deployment start time and therefore not reset by a retry. It first waits for the
controller to observe the requested generation and Release. Acceptance then
requires the exact desired image to be current, exactly two ready replicas, the
matching Service, `Reconciled`, `RuntimeReady`, `RouteReady`, `Healthy`, and
`Available` conditions at that generation, `Accepted=True` and
`ResolvedRefs=True` as reflected by the controller, and phase `Ready`.

After those controller-owned gates pass, the API performs an independent HTTPS
GET to `https://<application>.apps.lab.test<healthPath>`. It uses normal DNS,
hostname verification and the lab CA, follows no redirects, has a bounded client
timeout, and accepts only 2xx responses. Only then does one lease-checked database
transaction mark the Deployment `healthy`, set its completion time, and advance
`Application.current_release_id`; the scheduler subsequently marks the Operation
`succeeded`. Build, Release and failed Deployment history remain intact.

Controller progress and transient Kubernetes/network errors are retried within
the original deadline. Explicit controller rejection, route rejection or
unresolved references, controller-reported health failure, independent HTTPS
failure, and deadline expiry persist a terminal Deployment and Operation with a
specific uppercase machine code. An eligible failed runtime Deployment then
starts the automatic rollback described below.
Every state-changing database write still requires the live operation lease, so
a stale worker cannot complete a rollout after another replica has reclaimed it.

Kubernetes transport, timeout, conflict, and API availability errors requeue the
Operation. Admission rejection and `Reconciled=False` are terminal and fail the
Deployment intent without deleting the immutable Build or Release.

Agent-declared retryable failures (including host `flock` contention), HTTP
transport failures, and timeouts return the Build to `pending` and requeue the
Operation. Source/configuration, build, policy, and other non-retryable agent
failures make both Build and Operation terminal. Vulnerability-policy rejection
uses Build status `rejected`; other terminal failures use `failed`. No Release is
created on either path. Failure results retain an uppercase machine code,
bounded stage/status metadata, and retryability without storing logs or artifacts.

Scheduler logs and spans include operation/correlation context. Prometheus
metrics cover queue depth, claims and reclaims, per-process running work,
outcomes, lease-renewal failures, internal errors, execution duration, build
dispatch latency/outcomes, Release creation/reuse, Application CR actions,
controller-observation latency, deployment-acceptance duration, and bounded
controller/route/HTTPS acceptance-stage outcomes.
Metric labels are limited to bounded operation type, claim kind, outcome, and
scheduler stage values; operation and worker identifiers are never metric
labels.

Readiness results are cached for five seconds so probes do not turn PostgreSQL
or the Kubernetes API into per-request dependencies. PostgreSQL and Kubernetes
are required. The build agent is intentionally excluded: its unavailability
must stop future build execution, not all API traffic.

## Authentication and errors

The client token is 32 random bytes encoded as Base64. Provisioning retains the
plaintext token only in the external, mode-0600 operator secret file and places
only its lowercase SHA-256 digest in the Kubernetes Secret consumed by the API.
The service hashes the presented bearer token and compares digests in constant
time. Missing, malformed, and invalid credentials return `401` and a
`WWW-Authenticate: Bearer` header. Authorization headers are never logged.

All API failures use `application/problem+json`. Every problem contains
`type`, `title`, `status`, `detail`, `instance`, uppercase snake-case
`errorCode`, and the request `correlationId`. Dependency details, when useful,
are confined to an `extensions` object. A canonical UUIDv4 `X-Request-ID` is
accepted from the caller; otherwise one is generated. It is returned in the
response, attached to the active span, and included in structured request logs.

## Build-agent client boundary

`lap_platform_api.build_agent.BuildAgentClient` mirrors the existing build-agent
protocol with strict Pydantic request, success, and failure models. It uses the
lab root CA, the dedicated `platform-api-build-agent-client` certificate/key,
normal hostname verification for `build-agent.lab.test`, trace-context
propagation, a five-second connect timeout, and a bounded build-request timeout.
The deploy operation handler—not an HTTP route—calls `submit_build`; request
threads only record durable intent.

## Telemetry

Logs are one-line JSON on stdout and omit headers and secrets. OpenTelemetry
exports request spans, structured log records, request counts, and duration
histograms over OTLP/HTTP to the existing Collector. The Collector sends logs
to Loki, traces to Tempo, and OTLP metrics to its Prometheus exporter.
`/metrics` independently exposes bounded-label request, duration, and readiness
counters for direct Prometheus scraping through the `ServiceMonitor`.

## Runtime packaging and security

`platform/api/Dockerfile` uses a digest-pinned Python base and a hash-locked
runtime requirements export. The registry image is also referenced by digest.
The `platform-system` Deployment runs two non-root replicas with a read-only
root filesystem, dropped capabilities, seccomp, resource bounds, anti-affinity,
and rolling updates. Secrets are reconciled outside Git by
`tools/install-platform-api.sh`; the mTLS private key is group-readable only by
the pod security context. The Service is exposed only on the `api.lab.test`
HTTPS Gateway listener for `/api/v1`, using a cert-manager certificate from the
lab CA. Gateway ingress is scoped to the shared Envoy Gateway workload.

Ingress is limited to the platform namespace and monitoring. Egress is limited
to DNS, PostgreSQL, the Kubernetes API, the build agent, and the OpenTelemetry
Collector. Cilium's `kube-apiserver` entity handles the service-to-control-plane
translation used by readiness and Application reconciliation. The API service
account has only `get`, `create`, `patch`, and `delete` on cluster-scoped
Applications; it
cannot write Application status or create namespaces, Deployments, Services,
HTTPRoutes, Secrets, or NetworkPolicies. Worker hosts trust the private registry CA, and
the build host admits only lab control-plane/worker node addresses plus the
administration path; mTLS still gates the build-agent listener.

Build and install operations are:

```text
make platform-api-image
make platform-api-render
make platform-api-migrate
make platform-api-install
make platform-api-runtime-check
make platform-api-scheduler-check
make platform-api-build-dispatch-check
```

Re-running host and Kubernetes provisioning preserves the bearer token and
other external secrets and converges the same resources. `platform-api-migrate`
applies Alembic through a verify-full TLS port-forward before a deployment; API
pods never race each other running migrations.

## Automatic rollback

A deploy operation rolls back only after its new Release has changed the
Application CR desired state and runtime acceptance fails. Build, validation,
scan, and pre-apply failures never start a rollback. The target is the Release
that was current immediately before the attempt, provided a prior Deployment
proved it healthy through the full controller and trusted HTTPS acceptance path.
The API never searches farther back through Release history.

The failed Release Deployment stays failed. A separate `automatic_rollback`
Deployment links to it, restores the prior Release, and passes the same
generation, digest, two-replica, Service, route, controller, and independent
HTTPS gates. Only then is that Deployment healthy and the Application's current
Release restored. The deploy Operation remains failed and its `failureResult`
identifies the failed Release, rollback Deployment, restored Release, and
`rollbackStatus` (`succeeded`, `unavailable`, or `failed`). An unavailable
rollback has no invented Deployment. A failed rollback remains terminal and
requires operator intervention; it never starts another automatic rollback.

The active Operation lease gates every database transition. The failed
Deployment's one-to-one rollback link prevents duplicates on reclaim, and the
Application CR resourceVersion patch protects concurrent desired state. A
reclaimed worker resumes the existing rollback intent and does not patch again
when the restored spec is already present. Application and Deployment history
endpoints retain both attempts, and the Operation endpoint exposes the final
machine-readable outcome.

## Historical Release activation

`POST /api/v1/applications/{name}/rollbacks` accepts `{"releaseId":"<UUID>"}`.
The API requires an existing, non-deleted Application, a different current
Release, and a target Release from the same Application whose Build succeeded
and whose prior Deployment completed healthy. The request stores a queued
`rollback` Operation and a `manual_rollback` Deployment with the previous and
desired Release IDs. A repeated idempotency key returns that same intent.
Active-operation and active-Deployment database constraints reject concurrent
work for the Application.

The scheduler rechecks its lease and persisted intent, patches only the
Application custom resource, and uses the same controller generation, two
replica, route, and trusted HTTPS gates as a normal deploy. It then marks the
new Deployment healthy and makes the selected Release current in one
transaction. No Build runs and no Release is changed or recreated. An unknown,
foreign, current, or never-healthy Release is rejected. A failed manual
activation remains in Deployment and Operation history for operator repair;
it does not start an automatic chain to older Releases.
