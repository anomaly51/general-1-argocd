# The only secret-data permission: no list, metadata, app env, write, or wildcard.
path "kv/data/apps/cutline-studio/registry" {
  capabilities = ["read"]
}

# VSO renews its calling token immediately on login and during its lifetime.
# token_no_default_policy remains true, so grant this exact self operation.
path "auth/token/renew-self" {
  capabilities = ["update"]
}

# VSO validates a restored/tainted cached client using its own token identity.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
