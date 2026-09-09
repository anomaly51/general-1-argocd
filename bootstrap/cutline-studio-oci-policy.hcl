# No list, metadata, app environment, owner login, write, or wildcard permission.
path "kv/data/apps/cutline-studio/registry" {
  capabilities = ["read"]
}
