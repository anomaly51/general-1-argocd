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

Retrieve the unseal key only when it is needed:

```sh
security find-generic-password -w -s general-1-vault-unseal-key
```

Raft snapshots must be copied outside the Kubernetes cluster. The initial
bootstrap snapshot is stored outside this public repository with mode `0600`.
