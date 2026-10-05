# Environment management

## Deployment flow

Standalone Telegram bots use **main → CI → prod.yaml commit → automatic Argo CD sync**.
They have only `values/prod.yaml`, with `_release.policy: prod-only` and the exact
`sourceRepository`. Only a push to that repository's `main` may publish production.
CI retains the configured chart pin, updates immutable image digests, then waits
for the actual rollout. Pull requests run checks/builds only. There is no Promote
step and no dev/staging environment for these bots.

Other applications keep this flow:

1. Push source code to `dev`: CI builds/pushes image(s), obtains registry digests,
   then commits only `apps/<app>/values/dev.yaml` to GitOps main.
2. Push source code to `main`: same process for `staging.yaml`.
3. A signed GitHub push webhook refreshes the ApplicationSet immediately after
   the GitOps commit. Argo CD discovers profiles and automatically reconciles
   dev/staging. The normal polling interval remains a fallback.
4. Test staging and copy the full GitOps commit SHA from the CI release log.
5. GitOps → Actions → **Promote production** → **Run workflow**. Enter application
   and staging_commit, check release_verified, review dry_run first, then run with
   dry_run=false. It verifies the exact healthy staging release, retains prod
   configuration, commits prod.yaml, explicitly synchronizes production and waits
   for the current live deployments to have the expected image digests.

Production for other applications has no automatic synchronization. For them,
the manual workflow is the deployment button.
Authorized Argo administrators can still synchronize manually. Playground also
supports the branch-grouped previews described below.

The webhook is configured once on `general-1-argocd`; source repositories need no
additional webhook because their CI commits the release to GitOps. Its endpoint
is `https://argocd-webhook-general1.api-api-api.com/api/webhook`. Delivery triggers
reconciliation, while image download and application readiness still take time.
See [webhook operations](../utility-apps/argocd/webhook/README.md).

## GitHub / Vault access

Source jobs obtain a short-lived Vault token through GitHub OIDC. Vault checks
the source repository, allowed ref, event and trusted workflow identity. Bot roles
accept only main/push; other source roles accept main/dev. No
repository contains a personal access token. A GitHub App installed only on
`general-1-argocd` grants Contents write. Its private key is kept in Vault; Actions
creates a short-lived installation token and revokes it after the job.

Source CI can read Argo status, but receives no production synchronization token.
Only the manual GitOps production workflow can retrieve that separate Argo
project token. Vault paths: `ci/github-app`, `ci/harbor-applications`,
`ci/argocd-status`, `ci/argocd-production`. Runtime app secrets are separately scoped.
Runtime Vault policies also allow lookup, renewal and revocation of the caller's
own token; Vault Secrets Operator requires these even with default policy disabled.
Harbor and Argo credentials expire; rotate them in Vault before expiration.

## Optional environments

For applications other than standalone bots, an absent values profile means
build/publish only. To add an environment, create
its complete values profile, namespace/Vault role and isolated secrets first.
The ApplicationSet uses glob patterns, never an application list. Standalone bot
chart directories contain `bot`; their dev/staging paths are explicitly excluded
from discovery. CI also rejects these profiles and any bot deployment from dev.
Separate development tokens do not enable additional environments for these bots.

For a manual-production app without staging (such as Cutline), use source_commit
instead of staging_commit in Promote. Automatic prod-only bots reject Promote.
The workflow verifies immutable images published under main-sha-<12 characters>
and their full source revision. For prod-only OCI charts, also supply chart_version;
its source annotations and component digests must match. This path is rejected
if the application has a staging profile. It does not claim a staging test occurred.

## Release details

### Playground shared chart

For the eight playground applications, keep only `values/dev.yaml`,
`values/staging.yaml`, and `values/prod.yaml` under `apps/playground-<service>/`.
Set the chart source in each profile:

```yaml
_release:
  repository: harbor.internal.api-api-api.com/helm-charts
  chart: app
  revision: 0.6.0
  policy: promote
  namespace: playground-staging
  sourceRepository: anomaly51/playground-shell
```

Use the namespace and source repository for that profile. Put `image`, `env`,
resources and other chart values at the root of the file. Argo CD pulls `app`
from Harbor and receives the values through the Git-generated ApplicationSet.
`cluster/platform-helm-repository.yaml` registers the public OCI chart source.

To upgrade the chart, publish an exact version and its matching `app-v<version>`
Git tag in `anomaly51/platform-helm-charts`, then change the dev/staging pin.
In the service's source repository, run **Promote to prod** after checking staging. Promotion keeps
the production settings and copies the tested image digest and chart version;
it rejects changes to the chart repository or name. Image Updater continues to
write dev/staging image digests to these Git values files.

GitHub-hosted validation renders the public source tag for each referenced chart
version, without cluster credentials. Release validation renders the OCI package
from Harbor. The GitOps repository contains no playground chart archives.

Preview PRs with the same feature-branch name share one environment. At creation,
the workflow snapshots staging settings and chart versions; it replaces images
only for the participating PRs. Eight OCI app sources and seven Git-pinned utility
sources share the namespace. A preview lives for 15 minutes after readiness;
**Preview environments → refresh** renews or recreates it. Closing a component PR
restores its original staging image while other PRs remain open. Closing the last
PR, or lease expiry, removes the environment and its disposable data.

### Other applications

CI retries competing GitOps pushes without force push and rejects superseded
source builds. Image digests pin content even if a human changes a tag. Git
charts are pinned to a full GitOps SHA; OCI charts use a fixed version. The
ApplicationSet embeds the selected values from main into the pinned chart.
Promote copies only the built application image keys and chart revision;
production routes, replicas, resource limits, storage and Vault paths remain.

Web frontends use same-origin API routes so exactly the same browser bundle
works across environments. Shisha SSR uses a runtime SSR_API_URL. Cutline keeps
its audited build context, bounded upload, runtime video/browser smoke checks
and immutable Helm publishing; its old automatic production marker is retired.

Dry run never commits or synchronizes. A competing push during promotion fails
safely; rerun against fresh main. A failed production rollout is reported as a
failed workflow. There is no automatic rollback of database migrations or data.

## Configured environments

Source workflows are installed in all ten repositories. Standalone bot workflows
trigger only for main and pull requests targeting main; their sole GitHub
Environment is prod. Production uses pinned chart/image revisions.
Argo discovers isolated dev/staging profiles for Shisha backend/frontend, CRM,
Online Shop and Uptime Monitor. Uptime uses separate RabbitMQ users/vhosts; its
non-production bot has zero replicas and no Telegram token. Optional production
email, payment and Google OAuth credentials are excluded from these test profiles.
Cutline remains prod-only until separate owner-gated OIDC providers are configured.
Standalone Telegram bots are permanently prod-only under the current policy.
The GitHub App `anomaly51-gitops-ci` is installed only on `general-1-argocd`;
its validated private key is stored in Vault. Initial profile images are pinned
to digests published by the source CI workflows.

For example, promote Shisha backend in GitOps Actions with
`application=shisha-guid-backend`, `staging_commit=<full GitOps SHA>`,
`release_verified=true`, `dry_run=false`. Leave source_commit and chart_version
empty for this application. Use dry_run=true to review the proposed diff first.

Worker swap is managed by `utility-apps/maintenance/environments-swap`: dedicated
16 GiB encrypted disks on each of the three workers add 48 GiB of swap. Verified
totals are approximately 25/17/17 GiB, including earlier swap. Kubernetes LimitedSwap only
helps eligible Burstable containers and does not increase allocatable RAM.

Harbor's worker also has 64 GiB of additional persistent disk capacity, managed
by `utility-apps/maintenance/harbor-storage`, because image uploads exhausted its
original root disk. This storage is separate from swap and is included in VM
backups. The CI worker in the utility cluster received a separate 64 GiB expansion.


## Operational notes

Shisha dev/staging pull MinIO from the private Harbor mirror because the original
external image could not be pulled. The mirror was copied from the existing
production image cache and verified against its SHA-256 manifest and layer
digests. The one-time GitOps Job is in `utility-apps/maintenance/minio-mirror`.
Production's existing MinIO image reference was preserved.

The staging promotion path passed a real GitHub Actions dry run:
[CRM staging verification](https://github.com/anomaly51/general-1-argocd/actions/runs/36494908785).
This verifies credentials, the exact healthy staging rollout and the proposed
production diff; it does not perform a production rollout.

The control plane has also experienced intermittent k3s restarts following etcd
latency and leader-election timeouts. Argo may report temporary API errors during
these interruptions, including a misleading HTTP 403 when Kubernetes is not
ready. A deployment workflow fails rather than claiming a verified release; rerun
it once the API is healthy. Swap does not resolve control-plane disk latency.
