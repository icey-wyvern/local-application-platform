#!/usr/bin/env bash
# Only mount the newly attached staging disk; never a published parent.
set -euo pipefail
disk=${1:?expected newly attached disk}
[[ $disk =~ ^/dev/[a-zA-Z0-9]+$ && -b $disk ]]
[[ $(blockdev --getsize64 "$disk") == 64424509440 ]]
mapfile -t roots < <(lsblk -nrpo NAME,FSTYPE,LABEL "$disk" |
    awk '$2 == "ext4" && $3 == "cloudimg-rootfs" {print $1}')
[[ ${#roots[@]} == 1 ]] || { echo 'Expected exactly one ext4 cloudimg-rootfs partition' >&2; exit 1; }
mountpoint=$(mktemp -d /mnt/lap-image-XXXXXXXX)
mounted=false
cleanup() {
    if "$mounted"; then umount "$mountpoint" || return 1; fi
    rmdir "$mountpoint"
}
trap cleanup EXIT
mount -o rw "${roots[0]}" "$mountpoint"
mounted=true
grep -qx 'ID=ubuntu' "$mountpoint/etc/os-release"
grep -qx 'VERSION_ID="24.04"' "$mountpoint/etc/os-release"
[[ -d $mountpoint/etc/cloud/cloud.cfg.d ]]
[[ ! -e $mountpoint/var/lib/cloud/instance ]]
# The Azure image's DHCP hotplug definition also matches the only Hyper-V NIC
# and takes precedence over cloud-init's static netplan unit under networkd.
azure_hotplug=$mountpoint/etc/netplan/90-hotplug-azure.yaml
[[ -f $azure_hotplug ]]
grep -q 'hotpluggedeth0:' "$azure_hotplug"
grep -q 'driver: hv_netvsc' "$azure_hotplug"
rm -- "$azure_hotplug"
override=$mountpoint/etc/cloud/cloud.cfg.d/99-lap-nocloud.cfg
printf '%s\n' 'datasource_list: [ NoCloud ]' > "$override"
chown 0:0 "$override"
chmod 0644 "$override"
sync -f "$mountpoint"
umount "$mountpoint"
mounted=false
# Verify persisted contents through a new read-only mount without journal replay.
mount -o ro,noload "${roots[0]}" "$mountpoint"
mounted=true
[[ $(cat "$override") == 'datasource_list: [ NoCloud ]' ]]
[[ $(stat -c '%u:%g:%a' "$override") == '0:0:644' ]]
[[ ! -e $azure_hotplug ]]
umount "$mountpoint"
mounted=false
