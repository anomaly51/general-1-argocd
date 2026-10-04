# ARC remote builds

The utility ApplicationSet discovers each `utility-apps/<namespace>/<release>`
directory. Release names and namespaces preserve `runs-on: arc-runner-set`, the
restricted `juggluco-deploy` scale set, and
`tcp://arc-buildkit.arc-runners.svc.cluster.local:1234`.

Deploy the controller and Vault secret chart before the runner charts on a fresh
cluster. Registration reads only `kv/ci/arc-github` key `token`, transformed into
the `arc-runners/arc-github-token` Secret key `github_token`. Bind the Vault
Kubernetes role `arc-runners` to `arc-runners/vault-secrets`, audience `vault`,
with read-only permission on `kv/data/ci/arc-github`. No application credentials
are injected into general runner environments; workflows obtain their scoped
credentials from Vault with GitHub OIDC.

Runner/DinD images and all ARC chart dependencies are pinned. Explicit DinD uses
Kubernetes native sidecars (Kubernetes 1.29 or newer), bounded resources, and
size-limited ephemeral workspace/Docker volumes. Runners use `general-1-worker-1`
after its approved expansion to 6Gi RAM and a 60Gi disk. Each runner and its DinD
sidecar have a 512Mi memory limit, for at most 1Gi per pod. Two general jobs and
one restricted Juggluco job are the upper bound. Heavy image builds run on the
remote BuildKit worker; memory-intensive local tests may require a larger,
separately sized runner. Monitor concurrent workload memory; limits do not reserve
memory or account for unrelated workloads.

BuildKit uses one 30Gi `local-path` cache on `general-1-worker-3`; the deleted
cluster's cache is not required. Garbage collection targets 20GB maximum cache
usage and 10GB node free space, retaining at least 2GB. This is reclaimable-cache
policy, not a hard disk quota: active builds and other node data can exceed the
target. Monitor node free space and DiskPressure. The local-path PVC remains
node-bound and is disposable build cache, not a backup.

BuildKit TCP has no TLS authentication, so it is a ClusterIP service with an
ingress NetworkPolicy allowing only labeled runner pods in the same namespace
on port 1234. This requires the cluster's network-policy enforcement. Neither
the service nor runner Docker sockets are published externally. Both DinD and
BuildKit require privileged containers; these are trusted-code runners, not a
sandbox for untrusted pull requests. Keep public/fork PRs on GitHub-hosted runners
and preserve the restricted `Juggluco Deploy` runner-group policy. Organization
runner-group access rules are an additional GitHub-side control; this chart does
not change them.

The legacy ten-minute listener recycler and stale Harbor host aliases are not
carried over. Internal Harbor/Vault DNS and certificates are used normally.

Configuration references: [ARC 0.14.2 explicit DinD example](https://github.com/actions/actions-runner-controller/blob/gha-runner-scale-set-0.14.2/charts/gha-runner-scale-set/values.yaml),
[BuildKit v0.32.2 daemon settings](https://github.com/moby/buildkit/blob/v0.32.2/docs/buildkitd.toml.md),
and [remote BuildKit transport security](https://docs.docker.com/build/builders/drivers/remote/).
