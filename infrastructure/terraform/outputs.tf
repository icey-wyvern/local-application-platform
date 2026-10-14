output "switch_name" {
  description = "Created Hyper-V virtual switch name."
  value       = hyperv_virtual_switch.lab.name
}

output "nat_name" {
  description = "Created Hyper-V NAT name."
  value       = var.nat_name
}

output "virtual_machines" {
  description = "Lab virtual machines and fixed addresses."
  value = {
    for name, vm in hyperv_vm.vm : name => {
      id      = vm.id
      role    = local.vm_by_name[name].role
      address = local.vm_by_name[name].address
      vcpu    = local.vm_by_name[name].vcpu
      memory  = local.vm_by_name[name].memory_mib
    }
  }
}
