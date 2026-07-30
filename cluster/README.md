# Cluster

This is the single cluster entrypoint watched by the root Argo CD Application.

Keep Argo CD resources for this repository's cluster here:

```text
cluster/
  project.yaml
  applications/
  applicationsets/
```

The template starts with only `project.yaml`. Add `applications/` or
`applicationsets/` when you are ready to deploy paths from `apps/` or
`infrastructure/`.
