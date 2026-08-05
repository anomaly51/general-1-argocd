resource "vault_policy" "workload_kv_read" {
  name = "kubernetes-workload-kv-read"

  policy = templatefile(
    "${path.module}/policies/workload-kv-read.hcl.tftpl",
    {
      kubernetes_auth_accessor = vault_auth_backend.kubernetes.accessor
      kv_mount_path            = var.kv_mount_path
    }
  )
}

resource "vault_kubernetes_auth_backend_role" "workload" {
  for_each = local.workloads

  backend                          = vault_auth_backend.kubernetes.path
  role_name                        = each.key
  bound_service_account_names      = [each.value.service_account]
  bound_service_account_namespaces = [each.value.namespace]
  audience                         = "vault"
  token_policies                   = [vault_policy.workload_kv_read.name]
  token_ttl                        = 3600

  depends_on = [vault_kubernetes_auth_backend_config.kubernetes]
}
