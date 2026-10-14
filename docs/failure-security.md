# Failure and security validation

The lab validators exercise recovery and isolation on disposable workloads or
one service at a time. They restore any stopped service during cleanup. The
checks do not claim tolerance of simultaneous multi-node failure.

## Recovery behavior

| Fault | Verified outcome |
| --- | --- |
| API Pod lost during a Build | Another replica reclaims the leased Operation without duplicating its Build. |
| Controller Pod lost or owned resources drifted | The replacement reconciles Application desired state and repairs owned resources. |
| Build agent temporarily unavailable | The scheduler retries the durable Operation after the agent returns. |
| Registry temporarily unavailable during publication | The agent retries publication of the same scanned artifact within the build deadline. |
| PostgreSQL temporarily unavailable | API readiness fails while liveness remains available; operation history remains durable after recovery. |
| New Release fails HTTPS health | A separate automatic rollback Deployment restores the previous proven healthy Release. |

Registry publication has a finite retry deadline. If it expires, a new deploy
request is required after recovery. A failed rollback does not automatically
search older Releases. See [API behavior](platform-api.md) and
[backup and restore](backup-restore.md).

## Security checks

- Positive and negative Kubernetes RBAC checks cover API, controller and app
  ServiceAccounts. Application tokens are not automounted.
- Default-deny application policies block cross-application, API, link-local and
  plain HTTP paths while allowing required DNS, Gateway ingress and public
  HTTPS egress.
- API token checks, build-agent mTLS, registry authentication and trusted TLS
  reject unauthenticated or untrusted clients. Repository URL validation rejects
  private, loopback, link-local and credential-bearing inputs and unsafe
  redirects.
- Untrusted `platform.yaml` inputs fail validation before image publication.
  Application images must use immutable digests.
- Kubernetes Secret encryption at rest is validated on all three API servers.
- The only externally exposed Kubernetes Services are the intended Gateway
  LoadBalancer and scoped build-host telemetry NodePort.
- Credential-byte checks cover repository candidates, validation artifacts and
  bounded recent logs, metrics and traces. They do not prove the absence of a
  secret from every historical telemetry record.

The controller has narrowly scoped verbs, but Kubernetes RBAC cannot constrain
namespace creation by name prefix. Controller ownership checks provide the
additional boundary. App workloads retain intentionally available public HTTPS
egress. The build host, registry and PostgreSQL are lab components with finite
service-level recovery, not independently replicated services.
