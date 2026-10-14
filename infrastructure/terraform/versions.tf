terraform {
  required_version = ">= 1.15.9, < 1.16.0"

  required_providers {
    hyperv = {
      source  = "windsorcli/hyperv"
      version = "0.3.1"
    }
  }
}
