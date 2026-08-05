output "workload_bindings" {
  description = "Non-sensitive bindings expected by the Kubernetes GitOps layer."
  value = {
    for role, workload in local.workloads : role => {
      namespace       = workload.namespace
      service_account = workload.service_account
      vault_kv_path   = "${var.kv_mount_path}/${workload.secret_path}"
      service_account_annotation = {
        "vault.hashicorp.com/alias-metadata-secret_name" = basename(workload.secret_path)
      }
    }
  }
}

output "kubernetes_auth_accessor" {
  description = "Accessor embedded into the shared identity-templated policy."
  value       = vault_auth_backend.kubernetes.accessor
}
