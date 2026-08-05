locals {
  # This is the Vault authorization registry. Secret values are deliberately
  # absent: they are written directly to Vault and never enter Terraform state.
  workloads = {
    apps-vault-demo = {
      namespace       = "apps"
      service_account = "vso-vault-demo"
      secret_path     = "apps/vault-demo"
    }
    authentik = {
      namespace       = "authentik"
      service_account = "vso-authentik"
      secret_path     = "authentik/authentik"
    }
    monitoring-grafana = {
      namespace       = "monitoring"
      service_account = "vso-grafana"
      secret_path     = "monitoring/grafana-admin"
    }
    monitoring-pve-exporter = {
      namespace       = "monitoring"
      service_account = "vso-pve-exporter"
      secret_path     = "monitoring/pve-exporter"
    }
  }
}

check "workload_secret_paths" {
  assert {
    condition = alltrue([
      for workload in local.workloads :
      length(split("/", workload.secret_path)) == 2 &&
      dirname(workload.secret_path) == workload.namespace
    ])
    error_message = "Every workload secret path must be <namespace>/<secret-name>."
  }
}
