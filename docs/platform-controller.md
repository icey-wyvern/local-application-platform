# Application CRD and runtime controller

The platform controller is a separate Go `controller-runtime` service under
`platform/controller`. It owns the cluster API reconciliation boundary,
reconciles each application's namespace and workload resources, and attaches
the application to the shared Envoy Gateway.

## Application contract

`Application` is a cluster-scoped `platform.lab.test/v1alpha1` resource. Its
metadata name is the immutable, lowercase DNS-safe platform application name and
is limited to 48 characters. The spec is closed and contains exactly:

- `releaseId`: the durable platform Release UUID;
- `image`: an immutable OCI reference ending in `@sha256:<64 lowercase hex>`;
- `port`: an integer from 1024 through 65535;
- `healthPath`: a non-empty absolute path, at most 256 characters, with no query,
  fragment, or network-path (`//`) form;
- `replicas`: exactly `2`.

The structural CRD rejects missing, invalid, and unknown fields. The controller
repeats the desired-state validation defensively before taking any action.

## Status and conditions

The controller owns `status`. `observedGeneration` identifies the spec generation
that was processed. `desiredImage` follows the requested spec. `activeReleaseId`
and `currentImage` advance only when the desired digest is active in exactly two
ready replicas and the matching Service exists. That runtime state alone does not
make the Application Ready.

The controller reports these principal conditions:

- `Reconciled=True / RuntimeResourcesReconciled`: all owned resource specs match
  desired state.
- `RuntimeReady=True / WorkloadAndServiceReady`: the desired digest has exactly
  two ready and available replicas and the ClusterIP Service selects the workload.
- `RouteReady=True / RouteAccepted`: the current HTTPRoute generation has
  `Accepted=True` and `ResolvedRefs=True` for the intended Gateway parent.
- `Healthy=True / ExternalHealthCheckSucceeded`: a normally verified HTTPS GET
  to the application's external health URL returned a 2xx response.
- `Available=True / ApplicationHealthy`: all three readiness gates above are
  true; only then is phase `Ready`.
- while a gate is pending, `Available=False` and phase is `Pending`; explicit
  route rejection, unresolved references, or failed external health set phase
  `Error` without deleting the healthy workload.
- `False / InvalidSpec`: defensive validation rejected desired state. Phase is
  `Error`; admission normally prevents this state.
- during deletion, `Cleanup=False / NamespaceDeletionPending` records namespace
  cleanup progress.

Status patches are skipped when the effective status is unchanged, so repeated
reconciliation is idempotent and does not churn resource versions.

## Finalizer and recovery

The controller adds `platform.lab.test/application-finalizer` after validation.
On deletion it records cleanup status, deletes the owned namespace, waits for all
namespaced content to disappear, and only then removes the finalizer.
Ordinary API errors are returned to controller-runtime, which supplies its
rate-limited retry/backoff. Kubernetes persists the CR and finalizer, so a
replacement controller resumes reconciliation after restart.

The Deployment has one replica and leader election is disabled. A second replica
would add no availability guarantee without leader election, so the controller
remains at one replica.

## Runtime resources

An Application named `example` owns namespace `app-example`. The controller
reconciles deterministic `application` ServiceAccount, ResourceQuota, LimitRange,
Deployment and ClusterIP Service objects, a `registry-pull` Secret, and four
NetworkPolicies. Owner references and the
`platform.lab.test/application=<name>` label make ownership explicit.

The Deployment uses exactly two digest-pinned replicas, the declared port and
health path, 100m/128Mi requests, 1 CPU/512Mi limits, `maxUnavailable: 0`,
`maxSurge: 1`, a 300-second progress deadline, and hostname topology spread.
Pods use a dedicated token-less ServiceAccount, fixed non-root UID/GID 65532,
RuntimeDefault seccomp, dropped capabilities, no privilege escalation, and a
read-only root filesystem.

The quota permits the two steady replicas plus one rolling-update surge:
three Pods, 500m/512Mi aggregate requests, 3 CPU/1536Mi aggregate limits, one
Service and one Deployment. The LimitRange applies the same per-container
defaults and maximums as the workload contract.

## HTTP routing and external health

Each application owns an `application` HTTPRoute in its runtime namespace. It
uses hostname `<application>.apps.lab.test`, a `/` PathPrefix match, and sends
traffic to the `application` Service at the spec port. Its explicit parent is
Gateway `platform` in `envoy-gateway-system`, section `https`. The controller
never creates or changes that shared Gateway.

Route readiness is derived only from the parent status emitted by the Envoy
Gateway controller for the intended group, kind, namespace, name and listener.
Conditions for an older HTTPRoute generation are ignored. Reconciliation repairs
owned-field drift, recreates a deleted route, and avoids writes when desired state
already matches.

After workload, Service and route readiness, the controller requests
`https://<application>.apps.lab.test<healthPath>`. It uses normal DNS and hostname
verification against the mounted lab root CA, a three-second client timeout, at
most two attempts separated by 250ms, and accepts only 2xx responses. There is no
TLS bypass. Results are reused for 30 seconds to avoid event-driven probe loops;
the status records the checked route generation and check time.

## Registry credential boundary

The out-of-repository registry Docker config is installed only as the controller's
mounted Secret. It is mounted read-only with mode `0440`, validated during
startup, and copied to the deterministic `registry-pull` Secret in each owned
namespace. Secret reads bypass the shared controller cache, and the controller
does not list or watch Secrets. Plaintext registry credentials are neither logged
nor committed.

## Network isolation

Every application namespace starts with default-deny ingress and egress. Separate
policies permit:

- TCP and UDP DNS only to labelled CoreDNS Pods;
- ingress to the declared application port only from Envoy data-plane Pods owned
  by the platform Gateway in `envoy-gateway-system`;
- TCP 443 egress to public IPv4 destinations, excluding private, loopback,
  link-local, lab, Pod, Service, documentation, multicast and reserved ranges.

There is no general namespace, app-to-app, private-network, Kubernetes API, or
plaintext HTTP egress rule.

## RBAC and telemetry

The controller service account can manage only its Application status/finalizers,
the core resource types above, and namespaced HTTPRoutes. It has no Gateway read
or write access, node, storage, or unrelated workload authority. Namespace and
Secret creation
cannot be constrained by standard RBAC to a name prefix; all reads and mutations
in controller code nevertheless use deterministic names and verify ownership.

Default-deny network policy permits only Kubernetes API access, cluster DNS,
OpenTelemetry export, the shared Gateway VIP on TCP 443 for external health, and
Prometheus scraping of the metrics Service. Logs are structured JSON on stdout.
Reconciliations produce OpenTelemetry spans and the bounded
`lap_platform_controller_reconciliations_total{result=...}` and
`lap_platform_controller_health_checks_total{result=...}` metrics. Health latency
uses `lap_platform_controller_health_check_duration_seconds`;
controller-runtime's standard work-queue and reconcile metrics remain available.
Application, release, image digest, and other object identifiers are not metric
labels.

## Development and deployment

The repository pins Go, controller-tools, controller-runtime, envtest, and image
bases. Generate and validate with:

```sh
make platform-controller-generate
make platform-controller-check
kubectl kustomize cluster/platform-controller >/dev/null
```

Build, install, and exercise live reconciliation with:

```sh
make platform-controller-image
make platform-controller-install
make platform-controller-live-check
```

Installation applies the CRD and runtime manifests server-side. It creates the
controller's registry pull Secret from the existing out-of-repository credential;
the installer also creates a ConfigMap from the public lab root CA. No plaintext
credential or private key is stored in Git.

## Deliberate boundary

The API creates or updates only the five-field `Application` spec after a
successful Release. It does not write status or any runtime child resource. The
deploy scheduler waits for this controller's complete generation-specific
runtime, route, and health status, then performs its own normally verified HTTPS
health request before committing durable deployment success. This independent
acceptance check does not move runtime or Gateway ownership into the API.
Rollback is initiated by the API through a new Deployment intent; the controller
reconciles the resulting Application desired state through the same health gates.
