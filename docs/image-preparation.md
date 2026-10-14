# Ubuntu Hyper-V image preparation

## Source and capacity

Authoritative image settings live in `config/lab.yaml`. The pinned Canonical
Ubuntu Server 24.04 LTS AMD64 Azure archive is:

`https://cloud-images.ubuntu.com/releases/noble/release-20260826/ubuntu-24.04-server-cloudimg-amd64-azure.vhd.tar.gz`

Its SHA256 is `843d243792abb05b50e1a7f5e614e1184d8fc7195c119747cbb3038520258a22`.
Preparation checks both that pin and Canonical's
[`SHA256SUMS`](https://cloud-images.ubuntu.com/releases/noble/release-20260826/SHA256SUMS)
from the same dated release directory before extraction. No moving release alias,
assumed direct VHDX download, QEMU or qemu-img is used.

The source capacity is **32,213,303,296 bytes (30 GiB + 1 MiB)**. Preparation grows
the dynamic parent to **60 GiB (64,424,509,440 bytes)**. All nine differencing
children inherit that capacity. Guest cloud-init grows the root filesystem at
first boot; preparation does not shrink or resize Linux filesystems.

## Reproducible workflow

First run `make config-generate` in WSL. Then, from the repository root in
**elevated Windows PowerShell**, run:

```powershell
.\tools\prepare-ubuntu-hyperv-image.ps1
.\tools\prepare-ubuntu-hyperv-image.ps1 -VerifyOnly
```

The equivalent Make targets are `ubuntu-image-prepare` and `ubuntu-image-verify`,
provided Windows interop has an elevated administrator token. `make
ubuntu-image-fetch` remains an optional download/cache convenience; preparation
downloads missing archives itself and independently checks the checksums.

Preparation requires Windows Hyper-V PowerShell, Windows tar/fsutil, WSL VHD
mount support and the `Ubuntu-24.04` WSL distribution (`-Distribution` can select
another Ubuntu 24.04 installation). Linux uses existing bash, util-linux and
coreutils. Reserve over 30 GiB of temporary free space for the materialized raw
VHD, plus archive and VHDX space. Do not concurrently attach other WSL disks
while preparing: new-device discovery must yield exactly one disk.

The script automatically:

1. Verifies the pinned Canonical archive and its exact expected member.
2. Extracts into a unique staging directory; clears the NTFS sparse attribute
   only when present and verifies it cleared before conversion.
3. Uses `Convert-VHD -VHDType Dynamic`, then `Resize-VHD` to 60 GiB.
4. Attaches only the new staging VHDX through `wsl --mount --vhd --bare`, compares
   device lists, and requires exactly one ext4 partition labelled
   `cloudimg-rootfs` on the new disk. The pinned image also has a separate ext4
   `BOOT` partition, which is not the operating-system root.
5. Verifies Ubuntu 24.04 and an unbooted root, removes the Azure-only
   `/etc/netplan/90-hotplug-azure.yaml` DHCP definition that otherwise takes
   precedence over the lab's static `hv_netvsc` configuration, then writes
   root-owned mode 0644
   `/etc/cloud/cloud.cfg.d/99-lap-nocloud.cfg` containing:

   ```yaml
   datasource_list: [ NoCloud ]
   ```

6. Syncs/unmounts, remounts read-only without journal replay, checks the persisted
   content/ownership/mode, unmounts and detaches.
7. Checks format, dynamic type, exact capacity, attachment state and `Test-VHD`;
   hashes the result and publishes it with a `.prepared.json` receipt.

The new destination is:

`C:\Hyper-V\LocalApplicationPlatform\Virtual Hard Disks\ubuntu-24.04-server-cloudimg-amd64-nocloud-static-60g.vhdx`

An existing parent image is never silently adopted or modified. There is no
manual guest-image patch step.

## Idempotence and failure handling

An existing destination is never overwritten or mounted writable. Repeated runs
verify its receipt, full SHA256 and Hyper-V metadata, then return success. A
missing/mismatched receipt, changed hash, unexpected capacity, conversion error,
mount error or detach error fails closed. `-VerifyOnly` never prepares anything.
Make plan/apply targets depend on verification.

Failed staging VHDXs are retained for diagnosis; the unique extraction directory
is cleaned after detach. A failed detach reports the exact path and fails the
run. Resolve that attachment before retrying. An interrupted publication with no
receipt is not accepted: investigate and prepare under a new configured filename.
Never modify a published parent while children exist, including when VMs are off.
Recipe changes require a new filename and recipe version.

Receipts establish that the local result has not changed since this recipe's
verification; they do not claim bit-for-bit reproducible VHDX container metadata
or replace Canonical archive verification.

## Seeds, firmware and verification boundary

Terraform creates each `CIDATA` ISO with `user-data`, `meta-data` and static
`network-config`. Instance IDs are stable and unique per lab/VM name. Hostname,
admin user and the Windows public-key path come from generated authoritative
configuration. Only the configured user is declared; password login is disabled.
The network matches the Hyper-V `hv_netvsc` driver rather than assuming `ens3`.

Provider 0.3.1 declares Generation 2, Secure Boot enabled, template
`MicrosoftUEFICertificateAuthority`, SCSI disk/seed attachment and disk-first
boot. No manual firmware command is needed. Image preparation leaves VMs off.

Before trusting a booted lab guest, verify `cloud-init status --wait --long`,
`cloud-id`, configured hostname/user/key, root capacity, direct WSL SSH and the
connectivity matrix in [networking.md](networking.md). A prepared-image hash
check alone does not prove runtime connectivity.
