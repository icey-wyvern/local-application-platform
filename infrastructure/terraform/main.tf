provider "hyperv" {
  backend = "local"
}

locals {
  vm_by_name = {
    for vm in var.virtual_machines : vm.name => vm
  }

  prefix_length = tonumber(split("/", var.network_cidr)[1])
}

resource "hyperv_virtual_switch" "lab" {
  name                        = var.switch_name
  switch_type                 = "NAT"
  nat_name                    = var.nat_name
  nat_internal_address_prefix = var.network_cidr
  nat_host_address            = var.network_gateway
}

resource "hyperv_image_file" "base_image" {
  destination_path = "${var.vhd_root_path}\\${var.base_image_name}"
  keep_on_destroy  = true
}

data "hyperv_iso_volume" "seed" {
  for_each = local.vm_by_name

  volume_label = "CIDATA"

  files = {
    "meta-data" = yamlencode({
      instance-id    = "${var.lab_name}-${each.key}"
      local-hostname = each.key
    })
    "user-data" = templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
      hostname       = each.key
      ssh_user       = var.ssh_user
      ssh_public_key = trimspace(file(pathexpand(var.ssh_public_key_path)))
    })
    "network-config" = templatefile("${path.module}/templates/network-config.yaml.tftpl", {
      address       = each.value.address
      prefix_length = local.prefix_length
      gateway       = var.network_gateway
      dns_servers   = var.bootstrap_dns_servers
    })
  }
}

resource "hyperv_image_file" "seed_iso" {
  for_each = local.vm_by_name

  destination_path = "${var.seed_iso_root_path}\\${each.key}-seed.iso"
  content_base64   = data.hyperv_iso_volume.seed[each.key].content_base64
}

resource "hyperv_vhd" "vm_disk" {
  for_each = local.vm_by_name

  path        = "${var.vhd_root_path}\\${each.key}.vhdx"
  vhd_type    = "differencing"
  parent_path = hyperv_image_file.base_image.destination_path
}

resource "hyperv_vm" "vm" {
  for_each = local.vm_by_name

  name                 = each.key
  generation           = 2
  secure_boot          = true
  secure_boot_template = "MicrosoftUEFICertificateAuthority"

  cpu = {
    count = each.value.vcpu
  }

  memory = {
    dynamic       = false
    startup_bytes = each.value.memory_mib * 1024 * 1024
  }

  network_adapter = [{
    name        = "lab"
    switch_name = hyperv_virtual_switch.lab.name
  }]

  hard_disk_drive = [{
    path                = hyperv_vhd.vm_disk[each.key].path
    controller_type     = "SCSI"
    controller_number   = 0
    controller_location = 0
  }]

  dvd_drive = [{
    iso_path            = hyperv_image_file.seed_iso[each.key].destination_path
    controller_number   = 0
    controller_location = 1
  }]

  boot_order = [{
    type                = "hard_disk_drive"
    controller_type     = "SCSI"
    controller_number   = 0
    controller_location = 0
  }]

  state = {
    desired = "Off"
  }
}
