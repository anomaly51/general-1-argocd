# Monitoring Vault access

This chart owns the shared Vault access plumbing for the `monitoring` namespace:

- `ServiceAccount/vso-monitoring`
- `VaultConnection/vault`
- `VaultAuth/monitoring`

Workload-specific `VaultStaticSecret` resources live with their consuming
applications:

| Vault path | Owning chart | Kubernetes Secret |
| --- | --- | --- |
| `kv/monitoring/grafana-admin` | `monitoring/grafana` | `monitoring/grafana-admin` |
| `kv/monitoring/pve-exporter` | `monitoring/pve-exporter` | `monitoring/pve-exporter` |

This keeps the Vault path, destination Secret, and rollout target in the same
GitOps unit as the workload that consumes them.
