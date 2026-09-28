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
chart revision, namespace and CI release provenance; Argo strips it before Helm.
Production uses the existing application names (`apps-<app>`) and namespaces.
Dev/staging use separate namespaces and Vault credentials.

Source branches: dev → dev.yaml; main → staging.yaml. CI commits the new image
digest to this GitOps repository. Production is synchronized only by the manual
Promote production workflow, which copies release versions while retaining prod
settings and then checks the live rollout. See [management](../docs/environments.md).

Bootstrap status: discovery currently includes prod only. Dev/staging profiles
are prepared but will be activated after GitHub App access and the first safe CI
images are verified. Existing production images and chart pins are preserved.
