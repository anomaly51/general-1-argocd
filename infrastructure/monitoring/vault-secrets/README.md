# Monitoring secrets

Vault Secrets Operator authenticates with the `vso-monitoring` ServiceAccount
and synchronizes these Vault KV v2 paths into Kubernetes Secrets:

| Vault path | Kubernetes Secret |
| --- | --- |
| `kv/monitoring/grafana-admin` | `monitoring/grafana-admin` |
| `kv/monitoring/pve-exporter` | `monitoring/pve-exporter` |

The Vault policy is read-only and limited to these two paths. Workloads keep
their existing Secret names, so no application-specific configuration changes
are required.
