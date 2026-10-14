# Architecture at v1

The Windows 11 host runs nine Ubuntu VMs on a Terraform-owned internal Hyper-V
network. WSL Ubuntu is the administration workstation. The three control
planes use stacked etcd and an HA API VIP; three workers run Cilium, Longhorn,
Envoy Gateway, cert-manager, and the platform workloads. Ansible owns guest
services, Helm owns pinned add-ons, Kustomize owns platform Kubernetes objects,
and application code owns only its API, build, and reconciliation behavior.
The host uses Hyper-V directly, with 60 GiB thin-provisioned OS disks inherited
from a prepared parent image. WSL reaches the isolated lab through scoped
routing on Windows virtual interfaces.

## Request and deployment flow

`lap deploy` submits an authenticated, idempotent Operation. PostgreSQL stores
the intent before either API replica's scheduler claims it with a renewable
lease. The API asks the mTLS build agent to clone the validated public HTTPS
Git source, validate `platform.yaml`, build with rootless BuildKit, scan with
Trivy, and publish a digest-qualified image to the private registry. The API
persists one immutable Release per successful Build. It creates a Deployment
intent and patches the cluster-scoped Application CR. The controller alone
creates the namespace, workload, Service, route and policy. The API marks the
Deployment healthy and advances the current Release only after controller
status, two ready replicas, accepted routing, and an independent trusted HTTPS
health request pass. See [API](platform-api.md) and [controller](platform-controller.md).

An unhealthy new deployment leaves its failed Deployment and Release in
history. The API can automatically return to the immediately preceding proven
healthy Release, recording a separate Deployment. An explicit `lap rollback`
can activate any earlier Release of the same Application with healthy
Deployment history. It creates a new intent and uses the same lease, controller,
and health gates; it never rebuilds from Git. Neither path searches older
Releases automatically after a failed rollback. See [rollback semantics](platform-api.md#automatic-rollback).

## Boundaries and recovery

Public API and application traffic enters through named HTTPS Gateway routes.
The API has scoped Application CR permissions and cannot create controller
owned runtime resources or write Application status. Credentials remain outside
Git; the API stores a token hash, uses PostgreSQL TLS and build-agent mTLS,
and image references use immutable digests. Cilium policies and host firewalls
limit network paths. See [failure and security validation](failure-security.md).

Prometheus and Grafana provide metrics and alerts; the OpenTelemetry Collector
routes bounded logs and traces to Loki and Tempo. Telemetry is disposable.
PostgreSQL history and registry image bytes are backed up together during a
quiesced maintenance window. Restore checks exact configuration and versions,
restores durable stores, recreates Application desired state, and verifies API
and runtime health. It is a compatible-lab recovery procedure, not VM-image or
off-host disaster recovery. See [observability](observability.md) and
[backup and restore](backup-restore.md).

## Trade-offs and limits

Hyper-V keeps the full lab on one Windows host, and scoped WSL routing avoids
bridging or TCP relays. PostgreSQL Operations and leases avoid another queue, while
controller-owned resources avoid giving the API broad Kubernetes RBAC.
Manual activation uses persisted healthy Release artifacts; rebuilding from Git
would not prove historical recovery and could select different bytes. The
registry archive has checksums and a known-secret scan but no independent
authenticity signature or off-host copy. Public Git fixtures used by live
validation are temporary and are removed after each acceptance run.
