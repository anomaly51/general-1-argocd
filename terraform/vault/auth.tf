resource "vault_auth_backend" "kubernetes" {
  type        = "kubernetes"
  path        = var.kubernetes_auth_path
  description = "General-1 Kubernetes workload authentication"

  lifecycle {
    prevent_destroy = true
  }
}

resource "vault_kubernetes_auth_backend_config" "kubernetes" {
  backend                           = vault_auth_backend.kubernetes.path
  kubernetes_host                   = var.kubernetes_host
  disable_iss_validation            = true
  disable_local_ca_jwt              = false
  use_annotations_as_alias_metadata = true
}
