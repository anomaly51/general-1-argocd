# Shared GitHub Actions runners

Use `runs-on: arc-runner-set` for trusted organization jobs, including Juggluco.
The pool keeps one idle runner and scales to three total runners in GitHub's
Default runner group. Each job gets an ephemeral runner with its own bounded
Docker-in-Docker sidecar.

Keep public and fork pull requests on GitHub-hosted runners. The shared pool no
longer has Juggluco-specific runner-group isolation; retain trusted-branch checks
and scoped workflow credentials. Runner pods contain no application credentials.

## Files

- `values.yaml`: pool sizing, runner resources, and shared BuildKit settings.
- `templates/buildkit-*.yaml`: builder, cache PVC, and internal network access.
- `../secrets/`: Vault-backed GitHub registration token; unchanged.
- `../../arc-systems/arc-controller/`: ARC controller; unchanged.

Deploy the controller and Secrets before this chart on a fresh cluster.
Registration uses `kv/ci/arc-github` through `arc-github-token`. Keep the Vault
role bound to `arc-runners/vault-secrets` with read access to that path.

## Image builds

Connect Buildx with `driver: remote` and
`endpoint: tcp://arc-buildkit.arc-runners.svc.cluster.local:1234`.
The builder keeps its existing 30Gi cache PVC on `general-1-worker-3`.
Runner pods use `general-1-worker-2`; each runner plus DinD has a combined
1Gi memory limit. The builder has a 2Gi limit and runs at most two builds in
parallel. More jobs can queue build work without creating another builder.

The internal BuildKit service requires the runner pod label
`ci.api-api-api.com/buildkit-client: "true"`; its NetworkPolicy restricts ingress
to those pods in the same namespace. TCP transport has no TLS authentication,
so keep it internal and limit this pool to trusted code. Both DinD and BuildKit
need privileged containers.

Cache GC targets 20GB usage and 10GB free disk, retaining at least 2GB. These
targets are not a disk quota. Monitor free space and DiskPressure; the node-local
PVC stores disposable build cache, not application data.
