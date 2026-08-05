# Vault

This chart deploys a persistent, single-node Vault server backed by integrated
Raft storage. It is intentionally small enough for General-1 and can later be
expanded into a multi-node Raft cluster.

## Security boundary

- Vault is not running in development mode.
- The API and UI are cluster-internal services with no Ingress.
- A NetworkPolicy limits Vault listener traffic to `vault-system`.
- Internal TLS is currently disabled. Add a trusted internal certificate before
  exposing Vault outside the cluster or allowing direct application access.
- Vault uses Shamir sealing. A restarted Vault pod must be unsealed by an
  operator until an external KMS-based auto-unseal mechanism is configured.

## GitOps-managed workload access

This chart owns one Argo CD `PostSync` Job. The Job authenticates with a
short-lived projected Kubernetes token and reconciles two shared Vault objects:

- policy `kubernetes-workload-kv-read`;
- Kubernetes auth role `kubernetes-workloads`.

The policy derives the only readable KV v2 path from trusted Kubernetes
identity metadata:

```text
kv/<service-account-namespace>/<service-account-name>
```

Namespaces must carry the label
`secrets.hashicorp.com/vault-access=enabled`; both ApplicationSets apply it to
their destination namespaces. This lets a new workload be onboarded entirely
through its own GitOps chart without adding another policy or role here.

Vault cannot securely grant the reconciler its own initial privileges. After
initialization, the following trust anchor is created once by an administrator;
it is not an application onboarding step. The root token stays in macOS
Keychain and is never stored in Git or a Kubernetes Secret:

```sh
export VAULT_ADDR=http://127.0.0.1:8200
VAULT_TOKEN="$(security find-generic-password -w \
  -s general-1-vault-root-token)"
export VAULT_TOKEN

vault policy write gitops-vault-configurer - <<'EOF'
path "sys/auth/kubernetes" {
  capabilities = ["read"]
}
path "sys/policies/acl/kubernetes-workload-kv-read" {
  capabilities = ["create", "read", "update"]
}
path "auth/kubernetes/role/kubernetes-workloads" {
  capabilities = ["create", "read", "update"]
}
EOF

vault write auth/kubernetes/role/gitops-vault-configurer \
  bound_service_account_names=vault-configurer \
  bound_service_account_namespaces=vault-system \
  token_policies=gitops-vault-configurer \
  audience=vault \
  token_ttl=5m \
  token_max_ttl=5m

unset VAULT_TOKEN
```

## Operations

Bootstrap credentials are never committed to Git. On the workstation they are
stored in macOS Keychain under these service names:

- `general-1-vault-unseal-key`
- `general-1-vault-root-token`

To inspect Vault locally without exposing a Service:

```sh
kubectl --kubeconfig ~/.kube/configs/general-1-k3s.yaml \
  -n vault-system port-forward service/vault 8200:8200
```

After a Vault pod restart, unseal it without printing the key:

```sh
VAULT_UNSEAL_KEY="$(security find-generic-password -w \
  -s general-1-vault-unseal-key)"
kubectl --kubeconfig ~/.kube/configs/general-1-k3s.yaml \
  -n vault-system exec vault-0 -- \
  vault operator unseal "$VAULT_UNSEAL_KEY" >/dev/null
unset VAULT_UNSEAL_KEY
```

Raft snapshots must be copied outside the Kubernetes cluster. The initial
bootstrap snapshot is stored at
`~/.local/share/general-1-vault/backups/vault-bootstrap-0376e20.snap` with mode
`0600`.
