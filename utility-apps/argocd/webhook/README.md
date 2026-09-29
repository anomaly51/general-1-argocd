# GitHub ApplicationSet webhook

Endpoint: `https://argocd-webhook-general1.api-api-api.com/api/webhook`.
Configure one repository webhook on `anomaly51/general-1-argocd`: push events,
JSON content type, TLS certificate verification enabled. It refreshes the `apps`
and `utility-apps` Git generators after CI or a user pushes to main. It does not
grant permission to synchronize production. The normal Git polling remains a
fallback when a delivery fails.

Repository webhook ID: `688041086`. Delivery status and redelivery are available
in [GitHub webhook settings](https://github.com/anomaly51/general-1-argocd/settings/hooks/688041086).

The public HTTPRoute accepts only POST on exactly `/api/webhook` and forwards to
the ApplicationSet controller's existing port 7000. It does not expose Argo's
UI/API or controller metrics. GitHub signs deliveries using a random shared secret
stored as `secret` in Vault KV v2 `ci/argocd-webhook`.

The Vault role `argocd-webhook` is bound to service account `argocd-webhook` in
namespace `argocd`, audience `vault`. Its policy permits reading only
`kv/data/ci/argocd-webhook` and looking up, renewing or revoking its own token.
Vault Secrets Operator creates the separate `argocd-github-webhook` Secret.

Argo requires the key `webhook.github.secret` in its existing `argocd-secret`.
VSO 1.5 replaces a destination's entire data map even with `create: false`, so
it must not target that existing Secret. Instead, a Sync hook uses an atomic merge
patch to add only the webhook key, preserving Argo's password and signing key.
Its Role permits get/patch only on `argocd-secret` and the ApplicationSet
Deployment. A digest annotation triggers a controller reload only on secret
changes. The public route is applied after the hook verifies the controller
rollout; the Job prints no secrets.

To rotate: update the Vault secret, wait for VSO synchronization, sync this
Application to rerun its hook, then update the GitHub repository webhook with the
same secret. Verify a delivery/redelivery. A brief signature mismatch during
rotation does not disable the fallback Git polling.

Reference: [ApplicationSet webhook configuration](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/Generators-Git/#webhook-configuration).
