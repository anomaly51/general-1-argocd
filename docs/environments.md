# Products and environments

Current scope: configuration and management only. No new dev/staging workloads,
namespaces, databases, secrets or preview environments are deployed.

```text
apps/                                  # Existing Helm chart sources/defaults
  shisha-guid-backend/
  shisha-guid-frontend/
products/                              # Actual deployment inventory/configuration
  shisha-guide/
    environments/
      dev/                             # enabled: "false"
        deployment.yaml
      staging/                         # enabled: "false"
        deployment.yaml
      prod/                            # Existing production, enabled: "true"
        deployment.yaml
  weatherbot-for-tg51/environments/prod/
    deployment.yaml
cluster/applicationsets/apps.yaml
scripts/environments.py
.github/workflows/promote-production.yaml
.github/workflows/validate-environments.yaml
```

Only declared, enabled environments generate Applications. Omit directories for
environments a product does not need. Existing products are prod-only; Shisha Guide
also demonstrates disabled dev/staging. This inventory covers the ten Applications
already owned by ApplicationSet `apps`; standalone Juggluco and utility applications
keep their existing management.

`deployment.yaml` holds product, environment, enabled state, destination namespace
and component definitions. Each component separates `environmentValues` (hosts,
resources, Vault references, storage) from `releaseValues` (images). Argo CD merges
these into inline Helm values, with release versions taking precedence. This also
avoids the same-repository cross-revision `$values` limitation in the installed Argo
CD. `releaseValues` overrides chart image defaults. Git charts are pinned to full Git commit SHAs; Cutline's OCI
chart is pinned to the already deployed `0.1.12`, replacing the floating `0.1.*`.
Existing image tags are preserved by this migration; registry tag immutability/digest
support is not added here.

Production retains its existing Application names, Helm release names, namespace
`apps`, domains, volume claims and secrets. Backend/frontend remain separate Argo CD
Applications, grouped as one product through `app.kubernetes.io/part-of=shisha-guide`.
Filter `gitops.api-api-api.com/environment=prod` for environment views. This avoids
changing resource ownership or moving persistent data merely to reorganize files.

Charts in `apps/` are versioned sources; edit active environment settings in
`products/`. Editing an old chart's values.yaml on main does not deploy a pinned
release. A new chart commit must be explicitly selected as `chartRevision`.

## Manage and validate

```sh
python -m pip install -r scripts/requirements.txt
python scripts/environments.py list
python scripts/environments.py validate
python -m unittest discover -s tests -v
```

`enabled: "false"` excludes an environment entirely. It is not a pause switch:
disabling an already active environment removes its Applications and triggers their
cascading resource deletion. Persistent/external data follows its retention policy.
Do not toggle existing production to false to suspend updates.

Before enabling dev/staging, provision their own Vault roles and secret paths,
registry credentials, namespace gateway labels/policies and quotas through the
appropriate infrastructure management. Review external integrations and capacity.
The separate paths/domains in the scaffolds are planned configuration, not existing
secrets or DNS entries. No production data or credentials were copied.

## Production button

GitHub -> Actions -> **Promote production** -> **Run workflow** on `main`:

1. Product: `shisha-guide`.
2. `staging_commit`: full GitOps commit SHA whose staging release you tested.
3. Confirm `staging_verified` after checking Synced/Healthy and smoke tests.
4. Leave `dry_run=true` to inspect the diff; set false to commit the promotion.

The workflow copies **only the chart and image version fields** for all product
components from the selected immutable Git snapshot into the current prod descriptor.
It preserves prod namespace, domains, resources and `environmentValues`, including
all Vault configuration. The normal
repository `GITHUB_TOKEN` commits this change; no cluster credentials are needed.
Argo CD auto-sync subsequently applies the desired release. A successful workflow
commit is not proof of a completed rollout: check Argo CD health afterwards.

Disabled/missing staging is rejected, so the button cannot deploy these scaffolds.
Staging verification is an explicit operator attestation in this initial version,
not an automated health-check integration. The workflow has a `production` GitHub
Environment for optional required reviewers; configure protection rules as needed.
Repository write access can still change prod directly. Enforcing a button-only
policy also requires repository/Environment protection and a trusted CI writer.

## Source CI connection (not enabled in this change)

After an image is built and tested, source CI should update the corresponding
component's release fields in the GitOps repository:

- source branch `dev` -> product's `dev/deployment.yaml`;
- source branch `prod` -> `staging/deployment.yaml`;
- manual promotion -> `prod/deployment.yaml`.

Serialize writers and reject stale source commits. Publish an image before selecting
it in GitOps. Promote the same published artifacts; never rebuild for production.
Use release tags to identify tested source versions. Source repositories' current
workflows have not been changed, and automatic dev/prod branch routing is not yet
connected. A product without staging needs a separate explicit release-selection
workflow; this staging-promotion button intentionally refuses to invent a staging.

References: [Argo CD Git/List generator](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/Generators-List/),
[GitHub manual workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
