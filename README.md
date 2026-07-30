# GitOps Template Argo CD

Minimal Argo CD GitOps template.

## Layout

- `bootstrap/` contains the root Argo CD `Application` applied once with `kubectl`.
- `cluster/` is the single cluster entrypoint watched by the root Application.
- `cluster/project.yaml` defines the default Argo CD project for this repository.
- `cluster/applications/` is reserved for direct Argo CD `Application` resources.
- `cluster/applicationsets/` is reserved for Argo CD `ApplicationSet` resources.
- `apps/` contains reusable workload manifests, one directory per app.
- `infrastructure/` contains reusable cluster infrastructure manifests, one directory per component.

When the repo grows, add Argo CD `Application` or `ApplicationSet` resources under
`cluster/` to point at paths in `apps/` and `infrastructure/`.

## Bootstrap

Update repository placeholders in these files to point at the repository created from this template:

- `bootstrap/root-application.yaml`
- `cluster/project.yaml`

Then apply the root Application:

```bash
kubectl apply -f bootstrap/root-application.yaml
```

## Template State

This repository is intended for one Kubernetes cluster per Git repository. It
contains no preinstalled workloads or platform components.
