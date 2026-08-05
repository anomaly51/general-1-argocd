# Vault control plane

This module owns Vault configuration that Argo CD cannot reconcile:

- the existing Kubernetes auth mount and its configuration;
- one identity-templated KV read policy;
- one narrowly bound Kubernetes auth role per workload.

Argo CD still owns Vault, VSO, ServiceAccounts, `VaultAuth` objects, and
`VaultStaticSecret` objects. Real secret values are written directly to Vault;
this module never reads or writes them.

## State and credentials

Terraform state is stored in the `vault-system` namespace with the Kubernetes
backend and Lease-based locking. It contains configuration metadata, not KV
values. Back up the state together with Vault's Raft snapshots.

Supply access only through environment variables. Never add a token to HCL,
tfvars, a plan file, or Git:

```sh
export KUBE_CONFIG_PATH=/Users/nekoneki/.kube/configs/general-1-k3s.yaml
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN="$(security find-generic-password -w \
  -s general-1-vault-root-token)"
```

Use a local port-forward while Vault has no externally reachable TLS endpoint:

```sh
kubectl --kubeconfig "$KUBE_CONFIG_PATH" -n vault-system \
  port-forward service/vault 8200:8200
```

## One-time adoption

The cluster predates this module. Initialize the backend and import the
existing objects before the first plan:

```sh
terraform init
terraform import vault_auth_backend.kubernetes kubernetes
terraform import vault_kubernetes_auth_backend_config.kubernetes \
  auth/kubernetes/config

terraform import \
  'vault_kubernetes_auth_backend_role.workload["apps-vault-demo"]' \
  auth/kubernetes/role/apps-vault-demo
terraform import \
  'vault_kubernetes_auth_backend_role.workload["authentik"]' \
  auth/kubernetes/role/authentik
terraform import \
  'vault_kubernetes_auth_backend_role.workload["monitoring-grafana"]' \
  auth/kubernetes/role/monitoring-grafana
terraform import \
  'vault_kubernetes_auth_backend_role.workload["monitoring-pve-exporter"]' \
  auth/kubernetes/role/monitoring-pve-exporter

terraform import \
  'vault_policy.legacy_workload["apps-vault-demo"]' apps-vault-demo
terraform import \
  'vault_policy.legacy_workload["authentik"]' authentik
terraform import \
  'vault_policy.legacy_workload["monitoring-grafana"]' monitoring-grafana
terraform import \
  'vault_policy.legacy_workload["monitoring-pve-exporter"]' \
  monitoring-pve-exporter
```

Review `terraform plan`, then apply. After all four VSO resources authenticate
with the shared policy, delete `legacy-policies.tf` and apply again to remove
the obsolete policies.

## Normal workflow

```sh
terraform fmt -check -recursive
terraform validate
terraform plan -out=vault.tfplan
terraform apply vault.tfplan
```

For a new application, add one entry to `workloads.tf`, add its short
`vaultSecrets` values block to the application chart, and write values to
`kv/<namespace>/<secret-name>` through Vault UI or CLI. Never manage KV values
with Terraform because they would be persisted in state and plan files.

Unset the administrator token as soon as the operation finishes:

```sh
unset VAULT_TOKEN
```
