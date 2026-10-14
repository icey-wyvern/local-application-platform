# Windows, WSL and lab networking

The architecture is Windows 11 Pro with WSL 2 Ubuntu in **NAT mode**, plus a
Terraform-owned **internal** Hyper-V `lap-lab` switch. The lab subnet is
`10.20.0.0/24`, Windows gateway `10.20.0.1`. It is not an external/bridged
switch. NetNat provides VM internet egress; there are no inbound NAT mappings.

| Path | Mechanism |
| --- | --- |
| Windows to lab | Directly connected internal-switch host interface |
| WSL to lab | Route via current WSL Windows gateway; Windows forwards between virtual interfaces |
| Lab reply to WSL | Guest default gateway 10.20.0.1; Windows connected WSL route |
| VM to VM | Same internal switch/subnet |
| VM to internet | Guest default gateway and Terraform-owned NetNat |
| Physical LAN to lab | No published ports, external switch or physical-interface forwarding |

WSL's default NAT subnet changes across network recreation. Do not hard-code
172.x addresses or assume mirrored networking mirrors Hyper-V internal
adapters.

## Host bootstrap

Leave WSL's default NAT networking enabled. If `%UserProfile%\.wslconfig` already
selects another mode, merge the following into its existing `[wsl2]` section,
preserving other settings, then run `wsl --shutdown` after saving WSL work:

```ini
[wsl2]
networkingMode=nat
firewall=true
```

No change or shutdown is needed on the current default-NAT host. Require a WSL
version providing `wslinfo --networking-mode` and Windows Hyper-V
firewall cmdlets. Do not disable either Windows or Hyper-V firewall.

After a separately authorized Terraform apply has created the internal switch,
start the administration distro and run `make config-generate`. In elevated
Windows PowerShell at the repository root:

```powershell
.\tools\prepare-lab-routing.ps1
```

Or use `make lab-routing-prepare` when Windows interop has an elevated token.
This idempotent script:

- Requires NAT mode and discovers the distro's current IPv4/default gateway.
- Verifies the gateway belongs to a WSL virtual adapter and the lab interface
  has the exact authoritative gateway/prefix.
- Refuses physical-interface forwarding or disabled Windows firewall profiles.
- Creates/updates `LAP-WSL-to-lab` and `LAP-lab-to-WSL` Windows firewall rules,
  limited by interface, the current WSL `/32` and lab `/24`.
- Creates/updates `LAP-WSL-lab-outbound` for WSL's Hyper-V firewall creator ID,
  limited to lab destinations. Return traffic uses stateful filtering; it does
  not change default firewall actions or add general WSL inbound access.
- Enables IPv4 forwarding only on the WSL and lab virtual interfaces and
  installs/replaces the WSL lab route through its Windows gateway.

The script does not create or modify Hyper-V switches, NATs, VMs or disks. This
host-administration prerequisite is distinct from Terraform resource ownership.

For an existing lab after Windows or WSL restarts, run `make lab-start` from an
ordinary WSL shell. It checks the WSL route and TCP 6443 to the configured API
VIP first. If WSL can reach the API through its current gateway, it reconciles
DNS without elevation even when the default route supplies the lab path. If
Windows can reach the API but WSL cannot, it launches only `prepare-lab-routing.ps1`
through standard Windows UAC. It waits for completion and checks WSL again.
Declined UAC, failed repair, or an unavailable API fails startup clearly. It
reads the generated API VIP and network configuration; WSL address changes are
handled by the existing setup
script. No persistent elevated task, credential, relay or broad firewall rule is
added. Initial provisioning still uses the explicit elevated bootstrap above;
`make lab-start` is for a previously provisioned lab whose API is running.

Existing managed firewall policies can override local allows; investigate
effective policy rather than widening these rules.

## Bootstrap DNS and SSH

NetNat is not a DNS server. `network.bootstrap_dns_servers` supplies initial
recursive resolvers (currently 1.1.1.1 and 9.9.9.9); Terraform includes them in
NoCloud network-config. These resolve public bootstrap dependencies. Ansible
still owns later lab DNS and service configuration; these resolvers do not
serve `lab.test`.

`make lab-start` now reconciles WSL DNS after direct routing is reachable. It
uses the generated VIP and API hostname, writes `generateResolvConf=false` in
`/etc/wsl.conf`, and makes `/etc/resolv.conf` use the lab DNS VIP. dnsmasq
forwards non-lab queries, so ordinary `getent hosts api.lab.test` and HTTPS
clients work without a hosts entry. The WSL setting survives a distro restart;
run `make lab-start` after WSL networking changes to restore its route and DNS.
The script checks the resolved API address against the generated Envoy address
and does not disable TLS verification.

The private SSH key stays at `~/.ssh/lap_lab_ed25519`. Terraform reads
only the configured Windows copy of its public key. Use direct WSL OpenSSH with
that private key, `IdentitiesOnly=yes` and normal host-key verification. Verify
a new guest's host-key fingerprint through its trusted console before accepting
it. Do not copy the private key, disable host-key checking or add a proxy.

## Acceptance checks after authorized provisioning/boot

1. Windows: `Test-NetConnection 10.20.0.21 -Port 22` succeeds.
2. WSL: `ip route get 10.20.0.21` uses the WSL gateway; direct
   `ssh -i ~/.ssh/lap_lab_ed25519 -o IdentitiesOnly=yes ubuntu@10.20.0.21` succeeds.
3. Repeat direct SSH to all nine configured addresses. Verify `cloud-id` is
   `nocloud` and cloud-init finishes without errors.
4. From a guest, reach another lab guest, resolve `cloud-images.ubuntu.com`,
   and fetch `https://cloud-images.ubuntu.com/` over HTTPS.
5. Inspect the internal switch type, absence of NetNat inbound mappings and
   disabled forwarding on physical interfaces. No physical-LAN inbound is needed.

A successful Terraform plan cannot establish routing or firewall packet-path
correctness; verify those paths in the running lab.
