# Vault application access

Argo CD owns the Kubernetes side of the integration: the VSO connection and
each application's ServiceAccount, `VaultAuth`, and `VaultStaticSecret`.
HashiCorp Vault policies and Kubernetes auth roles are server-side Vault
configuration; VSO consumes them but does not create them.

Policy HCL is kept here as code. For this small cluster it is applied with the
official Vault CLI during application bootstrap. The administrator token and
secret values must never be committed. Move this directory to the official
Terraform Vault provider when role changes become frequent enough to require
stateful drift reconciliation.

## Current bindings

| Application | Namespace | ServiceAccount | Vault role/policy | Allowed KV v2 path |
| --- | --- | --- | --- | --- |
| Grafana | `monitoring` | `vso-grafana` | `monitoring-grafana` | `kv/monitoring/grafana-admin` |
| PVE exporter | `monitoring` | `vso-pve-exporter` | `monitoring-pve-exporter` | `kv/monitoring/pve-exporter` |

The policies authorize Vault API paths, so KV v2 uses `kv/data/...` in HCL even
though the CLI path is written as `kv/...`.

## Apply the current bindings

Run these commands from the repository root with `VAULT_ADDR` and a temporary
administrator `VAULT_TOKEN` set in the environment:

```sh
vault policy write monitoring-grafana \
  vault-config/policies/monitoring-grafana.hcl
vault write auth/kubernetes/role/monitoring-grafana \
  bound_service_account_names=vso-grafana \
  bound_service_account_namespaces=monitoring \
  policies=monitoring-grafana \
  audience=vault \
  ttl=1h

vault policy write monitoring-pve-exporter \
  vault-config/policies/monitoring-pve-exporter.hcl
vault write auth/kubernetes/role/monitoring-pve-exporter \
  bound_service_account_names=vso-pve-exporter \
  bound_service_account_namespaces=monitoring \
  policies=monitoring-pve-exporter \
  audience=vault \
  ttl=1h
```

Keep Vault's standard `default` policy attached. VSO uses its safe
`auth/token/renew-self` capability while managing short-lived client tokens.

## Add another application

For an application such as InfluxDB:

1. Store its values at `kv/<namespace>/<secret-name>` in Vault.
2. Add a narrow policy file such as
   `vault-config/policies/monitoring-influxdb.hcl`.
3. Create the matching Vault Kubernetes role, bound exactly to
   `vso-influxdb` in `monitoring`, with audience `vault`.
4. In `infrastructure/monitoring/influxdb`, add `ServiceAccount/vso-influxdb`,
   `VaultAuth/influxdb`, and one or more application-owned
   `VaultStaticSecret` resources.
5. Point the InfluxDB Helm values at the generated Kubernetes Secret. Never
   commit the actual secret values.

No ApplicationSet entry or shared secret registry is required.
