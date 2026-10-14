# Prerequisites

## Full Lab Host

The full lab supports one host architecture:

Windows 11 Pro physical host -> Hyper-V -> nine Ubuntu Server 24.04 LTS virtual machines.

The Windows host must have:

* Windows 11 Pro.
* Hardware virtualisation enabled in firmware.
* Hyper-V enabled.
* The current Windows user able to administer Hyper-V.
* WSL2 enabled.
* Ubuntu 24.04 LTS installed under WSL2.

The WSL2 Ubuntu environment must provide the Linux administration and development tooling:

* GNU Make.
* Ansible.
* kubectl.
* Helm.
* Kustomize.
* Python and uv.
* Normal Linux administration tools.

Install native WSL `uv` using the upstream standalone installer or another pinned local method. The repository Makefile uses native WSL `uv` and does not depend on Windows `uv.exe` through interop.

Make remains the top-level workflow. When Make runs under WSL2, Terraform targets invoke the Windows Terraform executable through Windows/WSL interoperability. Terraform uses the `windsorcli/hyperv` provider with the local backend because it manages the local Hyper-V host directly.

Terraform 1.15.9 is installed project-locally by:

```sh
make terraform-install
```

The installer downloads the Windows AMD64 Terraform zip and HashiCorp `SHA256SUMS` file, verifies the zip checksum, and extracts `terraform.exe` under `.local-tools/terraform/1.15.9/`. The `.local-tools/` directory is ignored by Git and must not be added to the Windows global PATH for this project.

The full lab must not require inbound connectivity from the wider physical LAN. The Hyper-V lab network is isolated behind host NAT while allowing Windows host to VM, WSL to VM, VM to VM, and VM to internet outbound connectivity.

Hyper-V resource creation and inspection that calls cmdlets such as `Get-VMHost`, `Get-VHD`, `Convert-VHD`, `New-VMSwitch`, `New-NetNat`, `New-VM`, `New-VHD`, and final Terraform `apply` operations require an elevated Windows administrator token for this local host design. Do not weaken UAC or broaden permissions to avoid that requirement.

## Guest Machines

All nine full-lab virtual machines run Ubuntu Server 24.04 LTS.

Terraform uses cloud-init NoCloud only for initial bootstrap:

* Hostname.
* Administrative user.
* SSH authorized key.
* Initial static networking required for Ansible.

Long-term guest configuration remains owned by Ansible.

The dedicated lab SSH identity is `~/.ssh/lap_lab_ed25519` in WSL.
Use it explicitly for SSH (with `-i`) and Ansible (with `--private-key`).
Copy the public key to the Windows user's `.ssh` directory and set
`hyperv.ssh_public_key_path` in the local `config/lab.yaml` to that Windows path
if the example's `~/.ssh/lap_lab_ed25519.pub` does not resolve to it. Terraform
runs natively on Windows. Keep the private key in WSL and out of the repository.
Copy `config/lab.example.yaml` to ignored `config/lab.yaml` before running Make
targets. Preserve that local file for backup verification and restore: existing
archives require its exact bytes, including the SSH public-key path.

## Base Image

Generate configuration before preparing the reusable Ubuntu base image:

```sh
make config-generate
```

Then run automated preparation from an elevated Windows PowerShell session:

```powershell
.\tools\prepare-ubuntu-hyperv-image.ps1
.\tools\prepare-ubuntu-hyperv-image.ps1 -VerifyOnly
```

This creates the configured Hyper-V storage directories and writes the final base image to:

`C:\Hyper-V\LocalApplicationPlatform\Virtual Hard Disks\ubuntu-24.04-server-cloudimg-amd64-nocloud-static-60g.vhdx`

See [image-preparation.md](image-preparation.md) for the source artifact, checksum verification method and validation requirement.

All OS disks are 60 GiB thin-provisioned differencing children of the prepared
parent image. Image preparation
requires WSL VHD mount support and over 30 GiB temporary extraction space.

WSL uses NAT mode. After an authorized apply creates the internal lab switch,
run elevated `tools/prepare-lab-routing.ps1` before direct WSL administration,
and use `make lab-start` from an ordinary WSL shell after a reboot or WSL
networking change to verify and restore it with UAC only when needed. See
[networking.md](networking.md) for scoped forwarding/firewall rules and checks.
Neither host preparation script is run during non-elevated repository validation.

The pinned Hyper-V provider cannot refresh a Generation-2 VM after Ubuntu adds
its UEFI file boot entry because that entry has no Hyper-V device object.
`make lab-destroy` therefore uses Terraform's last applied state without a
pre-destroy refresh. Inspect the destroy plan and keep the state file intact;
do not manually delete Terraform-owned lab resources.
