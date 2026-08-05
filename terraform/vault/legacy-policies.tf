# Migration-only resources. They keep the current policies in state while the
# roles switch to the shared identity-templated policy. Remove this file after
# fresh logins have been verified for every workload.
resource "vault_policy" "legacy_workload" {
  for_each = local.workloads

  name   = each.key
  policy = <<-EOT
    path "${var.kv_mount_path}/data/${each.value.secret_path}" {
      capabilities = ["read"]
    }
  EOT
}
