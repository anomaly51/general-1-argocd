# General-1 GitOps

Argo CD discovers applications from the directory tree; there is no central
application list.

```text
cluster/                         Argo CD projects and ApplicationSets
infrastructure/<namespace>/<app> cluster services, one Helm chart per app
apps/<app>                       ordinary workloads in namespace apps
shared/vault-secret/             reusable Vault-to-Kubernetes integration
```

Adding a directory under `infrastructure/<namespace>/` or `apps/` is enough for
the corresponding ApplicationSet to create an Argo CD Application.

## Adding a Vault-backed secret

1. Add `shared/vault-secret` as a local dependency in the application's
   `Chart.yaml`.
2. Add one short entry to that application's `values.yaml`:

   ```yaml
   vaultSecrets:
     enabled: true
     secrets:
       - name: influxdb
         destinationName: influxdb-credentials
   ```

3. Write the real values to `kv/<namespace>/influxdb` through the Vault UI,
   CLI, or API. Do not commit them.
4. Configure the application to consume Kubernetes
   `Secret/influxdb-credentials`.

The namespace and Vault path are derived automatically. No ApplicationSet,
Vault policy, Vault role, or central secret registry needs to be edited. See
[`shared/vault-secret`](shared/vault-secret/README.md) for the dependency path
and [`infrastructure/vault-system/vault`](infrastructure/vault-system/vault/README.md)
for the trust model.
