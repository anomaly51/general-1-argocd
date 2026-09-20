# Cutline Studio OCI deployment

The GitOps configuration selects OCI with ApplicationSet generator value
`cutlineOCIEnabled: "true"`. Only Cutline uses the conditional patch; its name,
project, destination, finalizer, and automated sync policy are unchanged.
The GitOps overlay omits exactly the API/frontend image tags, so each tested
chart version supplies those two tags while GitOps retains all other configuration.

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

## Deployment sources

The active source is Helm OCI repo URL without `oci://`, chart `cutline-studio`,
version constraint `0.1.*`. Its second source is the GitOps repository at `main`
with `ref: values` and no path. It supplies only
`$values/apps/cutline-studio/values.yaml`.

The canonical deployment chart lives in the private source repository
`anomaly51/cutline-studio` at `deploy/helm/cutline-studio`. Source CI publishes it
to `harbor.internal.api-api-api.com/cutline-studio/cutline-studio` with both
application image tags embedded in each release. Argo CD follows those releases.
The local chart files are a migration reference, not the active template source.

Rollback should publish a new higher chart version from an explicit source
revert; never replace an immutable chart.

## References

- [Argo CD 3.4 Git generator values](https://argo-cd.readthedocs.io/en/release-3.4/operator-manual/applicationset/Generators-Git/#pass-additional-key-value-pairs-via-values-field)
  and [templatePatch](https://argo-cd.readthedocs.io/en/release-3.4/operator-manual/applicationset/Template/#template-patch).
- [Argo CD 3.4 external Git values](https://argo-cd.readthedocs.io/en/release-3.4/user-guide/multiple_sources/#helm-value-files-from-external-git-repository)
  and [Helm version ranges](https://argo-cd.readthedocs.io/en/release-3.4/user-guide/tracking_strategies/#helm).
- [Argo CD v3.4.5 repository Secret examples](https://github.com/argoproj/argo-cd/blob/v3.4.5/docs/operator-manual/argocd-repositories.yaml)
  and [template merge implementation](https://github.com/argoproj/argo-cd/blob/v3.4.5/applicationset/controllers/template/patch.go).
- [Vault Secrets Operator destination/transformation API](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/vso/api-reference).
- [Installed VSO 1.5.0 token lifecycle implementation](https://github.com/hashicorp/vault-secrets-operator/blob/v1.5.0/vault/client.go)
  and [Vault self-token endpoints](https://developer.hashicorp.com/vault/api-docs/auth/token).

All cluster changes remain GitOps-driven.
