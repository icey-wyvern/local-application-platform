variable "lab_name" {
  type        = string
  description = "Project lab name used for resource tags and prefixes."
}

variable "lab_domain" {
  type        = string
  description = "Internal DNS domain for lab infrastructure."
}

variable "switch_name" {
  type        = string
  description = "Hyper-V virtual switch name."
}

variable "nat_name" {
  type        = string
  description = "Hyper-V NAT name for outbound VM connectivity."
}

variable "network_cidr" {
  type        = string
  description = "CIDR range for the isolated VM network."
}

variable "network_gateway" {
  type        = string
  description = "Gateway address for the isolated VM network."
}

variable "bootstrap_dns_servers" {
  type        = list(string)
  description = "Bootstrap recursive DNS resolvers; NetNat is not a DNS server."
}

variable "base_image_sha256" {
  type        = string
  description = "Pinned Canonical archive checksum used by image preparation."
}

variable "base_image_capacity_gib" {
  type = number
  validation {
    condition     = var.base_image_capacity_gib == 60
    error_message = "ADR 0002 requires a 60 GiB parent."
  }
}

variable "vhd_root_path" {
  type        = string
  description = "Windows host directory for dynamically expanding VHDX disks."
}

variable "seed_iso_root_path" {
  type        = string
  description = "Windows host directory for cloud-init NoCloud seed ISO media."
}

variable "base_image_source" {
  type        = string
  description = "Canonical Ubuntu Server 24.04 AMD64 Azure VHD archive URL used by the host image preparation process."
}

variable "base_image_name" {
  type        = string
  description = "Local base-image VHDX name."
}

variable "ssh_user" {
  type        = string
  description = "Initial SSH user configured by cloud-init."
}

variable "ssh_public_key_path" {
  type        = string
  description = "Path to the operator SSH public key injected by cloud-init."
}

variable "virtual_machines" {
  type = list(object({
    name       = string
    role       = string
    address    = string
    vcpu       = number
    memory_mib = number
    disk_gib   = number
  }))
  description = "Virtual-machine definitions generated from config/lab.yaml."

  validation {
    condition     = length(var.virtual_machines) == 9
    error_message = "The full lab must define exactly nine virtual machines."
  }
  validation {
    condition     = alltrue([for vm in var.virtual_machines : vm.disk_gib == var.base_image_capacity_gib])
    error_message = "Differencing disks must inherit the configured 60 GiB parent capacity."
  }
}
