# Reusable Vault secret

This local dependency removes repeated VSO manifests from application charts.
Each entry creates a `ServiceAccount`, `VaultAuth`, and `VaultStaticSecret`.

The identity name is also the final Vault path segment. The namespace comes
from the Argo CD destination, so this block:

```yaml
vaultSecrets:
  enabled: true
  secrets:
    - name: influxdb
      destinationName: influxdb-credentials
```

means:

```text
Vault:       kv/monitoring/influxdb
Kubernetes:  Secret/influxdb-credentials in monitoring
```

Add the dependency to an infrastructure application chart:

```yaml
dependencies:
  - name: vault-secret
    alias: vaultSecrets
    version: 0.1.0
    repository: file://../../../shared/vault-secret
```

For an application under `apps/`, use
`file://../../shared/vault-secret`. Run `helm dependency update <chart>` and
commit the generated `Chart.lock`.

The shared Vault role permits an identity to read only
`kv/<its namespace>/<its ServiceAccount name>`. Real values are written through
the Vault UI, CLI, or API and are never committed to Git.
