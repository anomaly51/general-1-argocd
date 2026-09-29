# Applications

One directory is one Helm chart and one application. Each existing values profile
declares an environment; no central application list or enabled flags are needed.

```text
apps/
  shisha-guid-backend/
    Chart.yaml
    templates/
    values/
      dev.yaml
      staging.yaml
      prod.yaml
  shisha-guid-frontend/
    Chart.yaml
    templates/
    values/
      dev.yaml
      staging.yaml
      prod.yaml
  bot-motivation/
    Chart.yaml
    templates/
    values/
      prod.yaml
```

Every profile is complete. There is no shared values.yaml. `_release` records
chart revision, namespace, deployment policy and CI provenance; Argo strips it before Helm.
Production uses the existing application names (`apps-<app>`) and namespaces.
Dev/staging use separate namespaces and Vault credentials.

For web applications: dev → dev.yaml; main → staging.yaml. CI commits the new image
digest to this GitOps repository. Production is synchronized only by the manual
Promote production workflow, which copies release versions while retaining prod
settings and then checks the live rollout. See [management](../docs/environments.md).

Dev/staging profiles are discovered automatically for Shisha backend/frontend,
CRM, Online Shop and Uptime Monitor. Their images are published by source CI.
Standalone Telegram bots have only prod profiles and `_release.policy: prod-only`:
main → build → commit prod.yaml → automatic deployment. Their source repository
is bound by `_release.sourceRepository`. Bot chart names contain `bot`; Argo
excludes their dev/staging profiles and CI rejects them. Promote is not used.
Cutline keeps manual production deployment. See the management guide for details.
