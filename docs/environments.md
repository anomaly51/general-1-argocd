# Environment management

## Deployment flow

1. Push source code to `dev`: CI builds/pushes image(s), obtains registry digests,
   then commits only `apps/<app>/values/dev.yaml` to GitOps main.
2. Push source code to `main`: same process for `staging.yaml`.
3. Argo CD discovers profiles and automatically reconciles dev/staging.
4. Test staging and copy the full GitOps commit SHA from the CI release log.
5. GitOps → Actions → **Promote production** → **Run workflow**. Enter application
   and staging_commit, check release_verified, review dry_run first, then run with
   dry_run=false. It verifies the exact healthy staging release, retains prod
   configuration, commits prod.yaml, explicitly synchronizes production and waits
   for the current live deployments to have the expected image digests.

Production has no automatic synchronization. An ordinary commit to GitOps main
does not deploy production; the manual workflow is the deployment button.
Authorized Argo administrators can still synchronize manually. No preview envs.

## GitHub / Vault access

Source jobs obtain a short-lived Vault token through GitHub OIDC. Vault checks
the source repository, main/dev ref, event and trusted workflow identity. No
repository contains a personal access token. A GitHub App installed only on
`general-1-argocd` grants Contents write. Its private key is kept in Vault; Actions
creates a short-lived installation token and revokes it after the job.

Source CI can read Argo status, but receives no production synchronization token.
Only the manual GitOps production workflow can retrieve that separate Argo
project token. Vault paths: `ci/github-app`, `ci/harbor-applications`,
`ci/argocd-status`, `ci/argocd-production`. Runtime app secrets are separately scoped.
Harbor and Argo credentials expire; rotate them in Vault before expiration.

## Optional environments

An absent values profile means build/publish only. To add an environment, create
its complete values profile, namespace/Vault role and isolated secrets first.
The ApplicationSet uses three glob patterns, never an application list. Do not
reuse a production database or Telegram token. Standalone Telegram bots stay
prod-only until separate dev/staging bot identities are supplied.

For a prod-only app, use source_commit instead of staging_commit in Promote.
The workflow verifies immutable images published under main-sha-<12 characters>
and their full source revision. For prod-only OCI charts, also supply chart_version;
its source annotations and component digests must match. This path is rejected
if the application has a staging profile. It does not claim a staging test occurred.

## Release details

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

## Bootstrap progress

The GitOps foundation is being installed before source workflows are activated.
Production discovery remains active with existing chart/image revisions.
Dev/staging discovery will be added after CI access and initial builds are
verified, so old browser images cannot accidentally address production APIs.
GitHub App setup currently requires the owner's GitHub Confirm access step.

Worker swap is managed by `utility-apps/maintenance/environments-swap`: dedicated
16 GiB encrypted disks supplement existing swap. Kubernetes LimitedSwap only
helps eligible Burstable containers and does not increase allocatable RAM.
