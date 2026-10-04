# Argo Workflows runtime

The General1 utility ApplicationSet deploys `argo-workflows-engine` in the
`argo-workflows` namespace. Argo CD installs the CRDs before the runtime and
retains them when someone removes this application. Use GitOps for deployment;
do not run `helm install` or apply manifests by hand.

We retain the former runtime version, v3.5.5, through upstream chart 0.41.1.
Plan a separate version upgrade before adding public access or untrusted users.
The controller manages workflows in this namespace, with a two-workflow limit.
We replace the chart's legacy executor role with `create`/`patch` access to
`workflowtaskresults`; workflow pods receive no `pods/exec` permission.

## Access

Connect with `rtk proxy connect-cluster general-1-k3s`, then open
`https://argo-server.argo-workflows.svc.cluster.local:2746` using the service
mapping. Argo serves HTTPS with its own certificate and requires Kubernetes
bearer-token authentication. We expose no Ingress, HTTPRoute, NodePort, or
LoadBalancer. Authentik integration needs a dedicated provider/client and a
Vault-backed secret before we add a route.

An operator can issue a short-lived token for service account `argo-viewer` in
`argo-workflows` through Kubernetes TokenRequest and enter `Bearer <token>` in
the UI. This account can view workflows and logs; it cannot submit workflows,
read secrets, or modify resources. Keep tokens out of Git, command transcripts,
and macOS Keychain. Administrators retain their existing Kubernetes access.

## Migration scope

We restore the runtime without submitting any Workflow, CronWorkflow, or Job.
We exclude these legacy WorkflowTemplates from utility-k3s:

- `terraform-apply-empty-k3s-cluster`
- `terraform-apply-k3s-cluster-argocd`
- `terraform-apply-proxmox-vm`
- `terraform-destroy-k3s-cluster`
- `terraform-destroy-proxmox-vm`

Review their embedded credentials, automatic destroy-on-error logic, and access
policies before rebuilding them. Rotate the exposed credentials through the
systems that issued them. This runtime does not need Proxmox or GitHub secrets.

We preserve `minio.minio-system.svc.cluster.local:9000` in the informational
`utility-migration-endpoints` ConfigMap. The old templates used that endpoint
for the `terraform-state` bucket. We configure neither a Terraform backend nor
an Argo artifact repository, and we create no buckets. Review a separate bucket
and scoped credentials before enabling workflow artifacts against restored
MinIO. The MinIO recovery belongs to its own GitOps application.

## Local validation

From this chart directory, run:

```sh
rtk helm dependency build
rtk helm lint .
rtk proxy ruby tests/test_render.rb
```

These commands render and validate manifests without changing the cluster.
After commit and push, verify `argo-workflows-engine` is Synced/Healthy and both
runtime Deployments are available. Confirm unauthenticated API requests fail
and the `argo-viewer` token can list workflows without gaining write access.

References: [upstream chart 0.41.1](https://github.com/argoproj/argo-helm/tree/argo-workflows-0.41.1/charts/argo-workflows),
[client authentication](https://argo-workflows.readthedocs.io/en/release-3.5/argo-server-auth-mode/),
[executor RBAC](https://argo-workflows.readthedocs.io/en/release-3.5/workflow-rbac/).
