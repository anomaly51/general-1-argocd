# General-1 GitOps

Argo CD discovers applications from the directory tree; there is no central
application list.

```text
cluster/                         Argo CD projects and ApplicationSets
infrastructure/<namespace>/<app> cluster services, one Helm chart per app
apps/<app>                       ordinary workloads in namespace apps
```

Adding a directory under `infrastructure/<namespace>/` or `apps/` is enough for
the corresponding ApplicationSet to create an Argo CD Application.

## Direct application values (temporary)

Credentials currently live as plain strings directly in workload templates and
are rendered as literal container environment variables. Credential data is not
passed through `values.yaml`; the repository does not create Kubernetes
`Secret` resources or use a shared secret chart.

This is a temporary and intentionally insecure setup: repository access grants
immediate access to every committed credential. Add new credentials only to the
consuming workload template's `env` entries until external secret management is
restored.
