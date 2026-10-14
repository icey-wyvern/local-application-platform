# Observability: metrics, logs and traces

## Architecture and ownership

The complete stack runs in `monitoring` and is reconciled with:

```text
make observability-install
make observability-check
make observability-failure-test
make observability-idempotence
```

Helm owns three pinned releases: kube-prometheus-stack chart `88.5.4`, Loki
chart `18.7.6` with Loki `3.7.6`, and Tempo chart `2.2.3` with Tempo `2.10.7`.
Generated Helm values come only from `config/lab.yaml` and
`config/versions.yaml`. Kustomize owns the OpenTelemetry Collector Contrib
`0.159.0` DaemonSet, RBAC, policies, monitors, dashboards, alerts, Grafana
Gateway resources and certificates.

The data paths are:

```text
Prometheus <- ServiceMonitor/platform metric endpoints
container stdout/stderr -> Collector filelog -> Loki -> Grafana
OTLP gRPC/HTTP -> Collector -> Tempo -> Grafana
```

There is one Collector agent on every Kubernetes node, including control-plane
nodes, so filelog covers static control-plane pods as well as worker workloads.
Agents read `/var/log/pods` read-only, run without Linux capabilities or a
runtime socket, and persist only their file offsets under
`/var/lib/otelcol-contrib`. The monitoring namespace already requires the
privileged Pod Security profile for node-exporter host access; this does not
make the Collector container privileged.

## Storage and retention

All stateful claims use the Longhorn V1/ext4 StorageClass with one replica on
each worker:

- Prometheus: 10 GiB, seven days, with an 8 GB size guard;
- Grafana: 2 GiB;
- Alertmanager: 2 GiB, with 120 hours of alert state;
- Loki: 5 GiB, 72-hour retention;
- Tempo: 5 GiB, 72-hour block retention.

Loki and Tempo are deliberately single-binary, single-replica lab deployments.
Their claims use `Retain` StatefulSet PVC retention semantics so release or pod
replacement does not discard telemetry. Telemetry remains disposable and is
outside the platform backup contract.

Loki uses filesystem-backed TSDB schema v13 and native OTLP ingestion. Tempo
uses its local trace backend and accepts only OTLP gRPC and HTTP. Neither uses
object storage, distributed mode, a cache tier, or a metrics generator.

## Collection, metadata and cardinality

The Collector uses file-backed offsets, Kubernetes metadata enrichment, a
memory limiter and batching. Fresh agents tail new log content; they do not
replay all historical node logs. Native Loki OTLP labels are limited to bounded
workload identity: service, environment, cluster, namespace, container and
controller names. Pod names, instance IDs, trace IDs, request IDs and other
correlation values remain structured metadata or log-body fields rather than
index labels.

Future custom components must:

- write JSON logs to stdout/stderr;
- send traces to `opentelemetry-collector.monitoring.svc.cluster.local` using
  OTLP gRPC `4317` or OTLP HTTP `4318`;
- set `service.name`, `service.namespace` and
  `deployment.environment.name=lab` resource attributes;
- propagate W3C trace context;
- include applicable `correlation_id`, `request_id`, `application_id`,
  `build_id`, `release_id` and `deployment_id` fields;
- never use those unbounded identifiers as Prometheus or Loki index labels.

The Collector adds cluster/environment identity and derives missing service
identity from Kubernetes container/namespace metadata. The validation workload
proved an emitted `trace_id` remains in the log body and links to the matching
Tempo trace.

## Grafana, dashboards and alerts

Grafana remains a ClusterIP service exposed only through Envoy Gateway at
`https://grafana.lab.test` with trusted lab TLS. Prometheus, Alertmanager, Loki,
Tempo and Collector services remain internal. Grafana declaratively provisions
healthy `prometheus`, `loki` and `tempo` datasources. Loki derived fields link
log trace IDs to Tempo; Tempo links traces back to matching Loki logs.

The version-controlled dashboards are:

- **Cluster Health**: readiness, resource use, pod health, API health and PVCs;
- **Platform Health**: platform component and storage health;
- **Telemetry Pipelines**: Collector, Loki and Tempo availability, accepted
  records/spans, export failures and ingestion throughput.

Rules cover the original node/API/workload/storage conditions plus Collector,
Loki and Tempo availability, persistent Collector export failures and telemetry
PVC pressure. Alertmanager remains local-only; external notifications are not
configured.

## Network and security boundaries

NetworkPolicy admits OTLP only from namespaces explicitly labelled
`telemetry.lap.dev/otlp=enabled`. Collector egress is limited to Loki, Tempo,
DNS and the Kubernetes API needed for metadata enrichment. Loki admits only
Collector ingestion plus Grafana queries and Prometheus scraping. Tempo admits
only Collector OTLP plus Grafana queries and Prometheus scraping. No
observability service creates a NodePort or LoadBalancer, and no host nftables,
physical-LAN, NAT or portproxy exposure was added.

## Validated behaviour and intentional limitations

Live acceptance queried a deterministic structured log from Loki and the known
trace ID from Tempo, then repeated both queries through Grafana. An unlabelled
namespace could not open an OTLP connection. Loki and Tempo pod replacement
preserved the known data and PVC identities; a Collector agent replacement
resumed ingestion. All five monitoring claims returned to healthy three-worker
Longhorn replication, and a second reconciliation retained Helm revisions,
PVC identities, Grafana credentials, dashboards and datasource identities.

Controller-manager, scheduler, etcd and kube-proxy remote scrapes remain
disabled because those kubeadm endpoints are not exposed. Cilium and MetalLB
metrics remain disabled in their established configurations; dashboards use
workload health for them. PostgreSQL uses StatefulSet readiness rather than a
new exporter. HAProxy has no exporter. These are intentional lab scope limits,
not missing pipeline functionality.
