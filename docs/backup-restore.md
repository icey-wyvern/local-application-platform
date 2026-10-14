# Lab backup and restore

`make lab-backup` writes `backups/<YYYYMMDDTHHMMSSZ>/lab-backup.tar.zst` on
the host. The timestamp uses basic UTC format. NTFS ACLs restrict the backup
directory and payloads to the current Windows user, SYSTEM and local
Administrators; WSL `chmod` alone does not restrict this DrvFS mount.
`backups/` is Git ignored. Copy the compressed
archive to separately protected storage after creation; this workflow does not
create an off-host copy.

## Contents and exclusions

The Zstandard tar contains a PostgreSQL 18 custom-format `postgresql.dump`, a
byte-for-byte tar of `/var/lib/lap-registry`, `config/lab.yaml`,
`config/versions.yaml`, selected generated DNS/Helm/platform environment
metadata, a Release image inventory, `manifest.json` with history counts and
component versions, and `SHA256SUMS` for every other member. The staging
directory beside the archive contains the same files for inspection. The
archive excludes Kubernetes Secrets, certificates, private keys, service
credentials, BuildKit cache, Terraform state, VM images and disposable
Prometheus/Loki/Tempo data. Restore generates or reconciles infrastructure and
credentials from the current compatible checkout and local secret stores.

The registry archive necessarily contains application image bytes. Treat the
whole archive as sensitive because untrusted images can contain data beyond
the platform's known credentials. Backup scans all staged members for known
local credential values and their Base64 forms before publication. Checksums
detect corruption and accidental modification; they are not an authenticity
signature against an attacker who can rewrite both archive and checksums.

## Consistency

Backup checks for running Operations, Builds and Deployments before changing
service state. It then scales the API to zero, waits for Pod termination,
rechecks work, stops the build agent and registry, streams `pg_dump -Fc` and
the registry filesystem tar, and restarts services in cleanup. API reads are
unavailable during the short maintenance window. If active work appears or
any stage fails, no final timestamped backup directory is published. Safe
temporary directories are removed. Queued Operations remain queued in the
dump; they resume through the normal scheduler after restore. A running
Operation at backup start makes backup fail, so no live lease is captured.

The backup verifies every historical Release's registry manifest link and
manifest blob digest. It refuses to publish a bundle with a missing historical
image.

## Restore prerequisites and procedure

Provision a compatible lab from the same authoritative configuration and
exact `config/versions.yaml`. Install PostgreSQL, the registry, platform API,
controller and their generated credentials/certificates with the existing
Make/Ansible/Kustomize automation. Keep the archive outside the checkout or
under ignored `backups/`. The current lab root CA and credentials may be newly
generated. Registry and database storage must have enough free space for
staging plus the prior registry directory.
The archive checks `config/lab.yaml` byte for byte. It is an ignored local copy
of `config/lab.example.yaml`; keep the original local file when verifying or
restoring an existing archive. The portable example cannot replace it for that
archive.

First verify the archive and exercise an isolated restore:

```sh
make lab-backup-check LAB_BACKUP_ARCHIVE=backups/<timestamp>/lab-backup.tar.zst
```

The command validates tar member types/names, checksums, exact versions and
lab configuration, Release inventory, registry manifest links/blobs, and a
custom-format dump parse before changing durable state. It then restores the
dump into a uniquely named disposable PostgreSQL database and extracts the
registry into a uniquely named disposable directory, compares all history
counts and image digests, and removes both targets in cleanup.

For a fresh target with no application history or Application resources:

```sh
make lab-restore LAB_BACKUP_ARCHIVE=backups/<timestamp>/lab-backup.tar.zst
```

If the target contains existing durable state, restore rejects it before
quiescing. After reviewing the archive and the replacement scope, explicitly
pass `LAB_RESTORE_CONFIRM=--confirm-replace` to replace that state. Restore
validates the archive and dump first, scales the API down, stops registry
writes, stages and swaps registry bytes, restores PostgreSQL in one transaction
as the application database role, checks Alembic and history counts, reapplies
the existing controller/API Kustomize resources, recreates Application desired
state from current Releases, verifies registry links, and waits for API
readiness. The final public check verifies the private CA over HTTPS and
expects an unauthenticated `401` from `/api/v1/info`; `/readyz` is checked by
the Deployment's internal readiness probe and is not exposed by the Gateway.
On a failure before the database transaction commits, it restores
the previous registry directory. On a failure after commit, it leaves the API
stopped and reports the partial restore for operator repair; do not submit new
work until both durable stores and Application resources are verified.
After confirming the API is stopped and inspecting the failed phase, retry the
same archive with `LAB_RESTORE_CONFIRM=--confirm-replace` and
`LAB_RESTORE_RESUME=--resume`. Resume repeats archive validation and the
production restore procedure; it is intended only for an interrupted restore
whose API replicas are already zero. Check the restored history, registry
digests, CLI access, Application reconciliation, and trusted HTTPS health
before accepting the recovered lab.
For the final historical-artifact acceptance, use `lap history <application>`
to select a Release that had a healthy Deployment before backup, deploy a newer
healthy Release, then run `lap rollback <application> --release <old-release-id>`.
Confirm the new `manual_rollback` Deployment is healthy, the Build/Release
counts did not increase during activation, the Application CR has two ready
replicas, its HTTPRoute is accepted, and its HTTPS health endpoint succeeds.

The repository acceptance command requires a disposable public HTTPS Git
fixture URL and its expected application name. It deploys the fixture, records
CLI history and HTTPS health, backs up, tests corruption rejection, restores
into isolated state and cleans up the fixture. Passing
`LAB_RESTORE_CONFIRM=--confirm-replace` additionally exercises replacement of
the live lab's durable state, then compares CLI history, Release ID, digest
and HTTPS health. This flag is an explicit destructive confirmation.

The workflow requires a full compatible lab, a healthy PostgreSQL service and
a reachable build host. It does not recreate VMs or serve as an off-host
disaster recovery system. Exact version/configuration matching is deliberately
strict; use the original pinned checkout or perform a separately planned
migration after restore.
