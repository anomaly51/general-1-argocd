# Cutline Studio OCI handoff

The committed bootstrap is inactive: ApplicationSet generator value
`cutlineOCIEnabled: "false"` preserves the existing Git chart source and both
deployed image tags. Only the Cutline application can use the conditional patch;
its name, project, destination, finalizer, and automated sync policy are unchanged.

## Credential bootstrap

The root Kustomization creates three resources in `argocd`: ServiceAccount,
VaultAuth, and VaultStaticSecret, all named `cutline-studio-oci`. This breaks the
private-chart bootstrap dependency: repository credentials do not come from the
private chart itself. VSO creates an Opaque Secret with Argo's `repository` label,
`type: helm`, `enableOCI: "true"`, the exact Harbor project URL, project
`gitops-apps`, and the existing pull-only robot's username/password. Raw Vault
payload and unrelated keys are excluded. TLS verification is not disabled.

Before committing the bootstrap, the authorized owner provisions these exact
non-secret definitions through the existing authenticated Vault helper:

- Policy name `cutline-studio-argocd-oci`: contents of
  `bootstrap/cutline-studio-oci-policy.hcl` at
  `sys/policies/acl/cutline-studio-argocd-oci` (`policy` string field).
- Kubernetes auth role `cutline-studio-argocd-oci`: JSON body from
  `bootstrap/cutline-studio-oci-role.json` at
  `auth/kubernetes/role/cutline-studio-argocd-oci`.
- Existing KV-v2 path `kv/data/apps/cutline-studio/registry` must contain
  `username` and `password` for the dedicated project pull-only robot. No new
  secret value or GitHub credential is required.

The role binds only ServiceAccount `cutline-studio-oci` in `argocd`, audience
`vault`, with a 600-second token TTL, maximum 3600 seconds, no default policy,
and read access to that one data path. The only additional permissions are
`update` on `auth/token/renew-self` and `read` on `auth/token/lookup-self`.
VSO 1.5.0 renews immediately after login and checks a restored/tainted cached
client through lookup-self. Without the explicit renewal rule, disabling the
default policy makes VSO fail with 403 before it can sync the registry Secret.
Keep `token_no_default_policy: true`; do not attach the broad default policy.
These self-only operations cannot manage other tokens. The role still cannot
read app env or owner-login credentials, list other paths, or write secret data.
Do not print secret data.
Verify VSO readiness and Argo repository connectivity without reading Secret data.

## Activation — separate owner-approved commit

Do not activate until source CI has published a tested immutable chart version
`0.1.<GITHUB_RUN_NUMBER>` to
`harbor.internal.api-api-api.com/cutline-studio/cutline-studio` and repository
authentication is healthy. The source publisher embeds both tested image tags
into that chart version. Chart publication must remain behind both image builds
and the bounded real-codec runtime test.

In one reviewed GitOps activation commit:

1. Set only `spec.generators[0].git.values.cutlineOCIEnabled` in
   `cluster/applicationsets/apps.yaml` from string `"false"` to string `"true"`.
2. Remove exactly `images.api.tag` and `images.frontend.tag` from
   `apps/cutline-studio/values.yaml`; retain repositories, pull policies, every
   other image tag, and all runtime/security/storage/migration/replica values.
   These two removals let the tested chart defaults own the application images.
3. Retire the legacy GitHub scheduled two-tag writer before it can write tags
   back into the overlay. Do not run it manually as a deployment substitute.

The active source is Helm OCI repo URL without `oci://`, chart `cutline-studio`,
version constraint `0.1.*`. Its second source is the GitOps repository at `main`
with `ref: values` and deliberately **no path**. It supplies only
`$values/apps/cutline-studio/values.yaml`, avoiding a second set of rendered
resources. No broad ApplicationSet or cluster polling configuration is changed.

Before pushing activation, render the packaged chart with the modified overlay
and compare resource identity/configuration with the existing chart. Keep the API
at zero until the separately authorized capacity/startup work allows resumption.
After GitOps reconciliation, verify the same Application UID/finalizer, two
sources, selected chart revision, exact images, and no repeated resources. A
paused API is not deployment success: require one current/updated/ready/available
API replica and the authenticated version endpoint.

Prove subsequent automatic delivery using a second source commit: tested images
and a newer chart must appear, then Argo's normal reconciliation must select that
chart and make the new images healthy without dispatch, tag writeback, manual
sync, or forced refresh. The usual reconciliation interval is minutes, not an
instant delivery guarantee. Rollback should publish a new higher chart version
from an explicit source revert; never replace an immutable chart.

## Local validation

Run `rtk ruby scripts/cutline-studio-oci.test.rb` and
`rtk kubectl kustomize cluster`. Tests render the real Go template expressions
through Helm locally, validate both activation states and unrelated apps, and
check the exact scoped Vault/Argo credential contract. They do not contact Vault,
publish artifacts, mutate cluster resources, or touch user jobs/media.

Optional exact-controller merge validation: set `CUTLINE_ARGO_CONTEXT` to an
already authenticated owner Argo context and `CUTLINE_ARGO_KUBECONFIG` to the
explicit General-1 kubeconfig when running the Ruby tests. The test uses only
the non-mutating Argo Generate RPC with a local list fixture and temporary
service port-forward, no Git/Harbor fetch. Argo requires ApplicationSet-create
permission even for this read-only RPC; never expand the CI token to run it.
It asserts that the actual controller merge removes `spec.source`, retains two
sources and the same identity/finalizer/sync policy. The CLI has a 30-second
bound and its temporary artifacts are removed. Without the optional context,
this one integration test is explicitly skipped.

## Confirmed upstream contracts

- [Argo CD 3.4 Git generator values](https://argo-cd.readthedocs.io/en/release-3.4/operator-manual/applicationset/Generators-Git/#pass-additional-key-value-pairs-via-values-field)
  and [templatePatch](https://argo-cd.readthedocs.io/en/release-3.4/operator-manual/applicationset/Template/#template-patch).
- [Argo CD 3.4 external Git values](https://argo-cd.readthedocs.io/en/release-3.4/user-guide/multiple_sources/#helm-value-files-from-external-git-repository)
  and [Helm version ranges](https://argo-cd.readthedocs.io/en/release-3.4/user-guide/tracking_strategies/#helm).
- [Argo CD v3.4.5 repository Secret examples](https://github.com/argoproj/argo-cd/blob/v3.4.5/docs/operator-manual/argocd-repositories.yaml)
  and [template merge implementation](https://github.com/argoproj/argo-cd/blob/v3.4.5/applicationset/controllers/template/patch.go).
- [Vault Secrets Operator destination/transformation API](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/vso/api-reference).
- [Installed VSO 1.5.0 token lifecycle implementation](https://github.com/hashicorp/vault-secrets-operator/blob/v1.5.0/vault/client.go)
  and [Vault self-token endpoints](https://developer.hashicorp.com/vault/api-docs/auth/token).

The installed General-1 VSO CRD was also checked read-only for destination labels
and `excludeRaw` support. All cluster changes remain GitOps-driven.
