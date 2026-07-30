# General-1 GitOps

Minimal Argo CD configuration for the General-1 cluster.

The cluster currently runs:

- `nfs-provisioner` — an NFS dynamic provisioner and the `nfs-client`
  StorageClass;
- `dependency-demo` — a single nginx pod whose content is stored on an
  NFS-backed PVC.

## How it works

`gitops-root` watches `cluster/`. Two ApplicationSets then discover Helm charts:

- `infrastructure/*/Chart.yaml` → infrastructure Applications;
- `apps/*/Chart.yaml` → workload Applications.

For workload charts, `Chart.yaml.name` is used as the Argo CD Application,
Helm release, and namespace name. The NFS chart is the only explicit exception:
it is installed as release `nfs-subdir-external-provisioner` in `nfs-system`.

ApplicationSet sync waves express the intended infrastructure-before-workloads
order, but generated Applications reconcile independently. The demo's real
dependency gate is its PVC: it remains pending until `nfs-client` and the NFS
provisioner are ready.

## Shared storage

`nfs-client` is a cluster-wide, opt-in StorageClass backed by
`192.168.1.13:/export/test`. Applications create namespaced PVCs and Kubernetes
creates the matching PVs automatically. Do not create PVs per application by
hand.

The default StorageClass remains `local-path`: use it for disposable,
node-local data. Select `nfs-client` explicitly when data must be shared across
nodes with `ReadWriteMany` or retained after a PVC is removed.

See [docs/storage.md](docs/storage.md) for a copy-paste PVC example, mounting
instructions, lifecycle behavior, and NFS limitations.

## Add a component

1. Add a Helm chart under `infrastructure/<name>/` or `apps/<name>/`.
2. Give it a unique `name` in `Chart.yaml`.
3. Push to `main`; the matching ApplicationSet creates and syncs the
   Application.

No additional Argo CD Application manifest is required.

## Bootstrap

Argo CD must already be installed. Create the project before the root
Application because `gitops-root` belongs to that project:

```bash
kubectl apply -f cluster/project.yaml
kubectl apply -f bootstrap/root-application.yaml
```

## Local checks

```bash
helm dependency build infrastructure/nfs-provisioner
helm lint --strict infrastructure/nfs-provisioner
helm lint --strict apps/dependency-demo
kubectl kustomize cluster
```

The demo workload is disposable. Removing its chart from `main` prunes the
generated Application and PVC, but the NFS PV and its data remain available
for manual recovery or cleanup.
