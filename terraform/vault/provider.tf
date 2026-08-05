provider "vault" {
  # VAULT_ADDR and a short-lived VAULT_TOKEN are supplied by the caller.
  # Credentials never belong in Terraform configuration or state.
}
