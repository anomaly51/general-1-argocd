variable "kubernetes_auth_path" {
  description = "Vault mount path for this cluster's Kubernetes auth method."
  type        = string
  default     = "kubernetes"
}

variable "kubernetes_host" {
  description = "Kubernetes API URL reachable from the Vault pod."
  type        = string
  default     = "https://kubernetes.default.svc:443"
}

variable "kv_mount_path" {
  description = "Vault KV v2 mount containing workload secrets."
  type        = string
  default     = "kv"
}
