# Mock provider only: these tests must never access Hyper-V or build seed ISOs.
mock_provider "hyperv" {}

variable "virtual_machines" {
  type = list(object({
    name       = string
    role       = string
    address    = string
    vcpu       = number
    memory_mib = number
    disk_gib   = number
  }))
}

run "nocloud_and_firmware" {
  command = plan
  variables {
    ssh_public_key_path = "tests/admin.pub"
  }
  assert {
    condition = alltrue([for name, seed in data.hyperv_iso_volume.seed :
      toset(keys(seed.files)) == toset(["user-data", "meta-data", "network-config"]) &&
      seed.volume_label == "CIDATA" &&
      yamldecode(seed.files["meta-data"])["instance-id"] == "${var.lab_name}-${name}" &&
      yamldecode(seed.files["meta-data"])["local-hostname"] == name &&
      yamldecode(seed.files["user-data"]).hostname == name &&
      yamldecode(seed.files["user-data"]).users[0].name == var.ssh_user &&
      yamldecode(seed.files["user-data"]).users[0].ssh_authorized_keys[0] == trimspace(file("tests/admin.pub")) &&
      yamldecode(seed.files["user-data"]).ssh_pwauth == false &&
      yamldecode(seed.files["network-config"]).ethernets.lab.addresses[0] == "${local.vm_by_name[name].address}/24" &&
      yamldecode(seed.files["network-config"]).ethernets.lab.match.driver == "hv_netvsc" &&
      tolist(yamldecode(seed.files["network-config"]).ethernets.lab.nameservers.addresses) == var.bootstrap_dns_servers
    ])
    error_message = "Every CIDATA seed must contain config-derived user, metadata and static networking."
  }
  assert {
    condition     = length(toset([for seed in data.hyperv_iso_volume.seed : yamldecode(seed.files["meta-data"])["instance-id"]])) == 9
    error_message = "Each VM needs its own NoCloud instance-id."
  }
  assert {
    condition = alltrue([for vm in hyperv_vm.vm :
      vm.generation == 2 && vm.secure_boot && vm.secure_boot_template == "MicrosoftUEFICertificateAuthority" &&
      vm.boot_order[0].type == "hard_disk_drive" && vm.hard_disk_drive[0].controller_type == "SCSI"
    ])
    error_message = "Firmware must match the successful Ubuntu smoke test."
  }
  assert {
    condition = alltrue([for disk in hyperv_vhd.vm_disk :
      disk.vhd_type == "differencing" && disk.parent_path == hyperv_image_file.base_image.destination_path
    ]) && hyperv_virtual_switch.lab.nat_host_address == var.network_gateway
    error_message = "Disks must inherit the verified parent and NAT must use a plain gateway address."
  }
}

run "configured_admin_user" {
  command = plan
  variables {
    ssh_public_key_path = "tests/admin.pub"
    ssh_user            = "labadmin"
  }
  assert {
    condition = alltrue([for seed in data.hyperv_iso_volume.seed :
      yamldecode(seed.files["user-data"]).users[0].name == "labadmin" &&
      length(yamldecode(seed.files["user-data"]).users) == 1
    ])
    error_message = "Seeds must honor the configured administrator rather than hard-code ubuntu."
  }
}

run "reject_incompatible_parent_capacity" {
  command = plan
  variables {
    ssh_public_key_path     = "tests/admin.pub"
    base_image_capacity_gib = 30
  }
  expect_failures = [var.base_image_capacity_gib]
}

run "reject_incompatible_child_capacity" {
  command = plan
  variables {
    ssh_public_key_path = "tests/admin.pub"
    virtual_machines    = [for vm in var.virtual_machines : merge(vm, { disk_gib = 30 })]
  }
  expect_failures = [var.virtual_machines]
}
