# Hermes Agent

One official Hermes v0.23.2 container with a small LAN proxy in namespace `hermes`, managed by Argo CD.
`templates/resources.yaml` contains the complete deployment, with the image pinned by digest.

Chat uses the ChatGPT/Codex subscription provider and `gpt-6.1-sol`, matching
the current local Codex model. Hermes has its own OAuth session.

Start a chat:

```sh
kubectl -n hermes exec -it deployment/hermes -- hermes
```

The internal API is `http://hermes.hermes.svc.cluster.local:8642/v1`.
Its bearer key is in Vault `kv/apps/hermes`, field `API_SERVER_KEY`.
Open **https://hermes.internal.api-api-api.com** from the LAN (or a VPN with
access to the LAN and its internal DNS). There is no browser login and no public
route. All devices with access to this internal endpoint share the same agent.

The internal Gateway terminates HTTPS. A small Nginx sidecar forwards a deployment
session token to Hermes on loopback; the token stays server-side in Vault
`kv/apps/hermes`, field `HERMES_DASHBOARD_SESSION_TOKEN`. Hermes' built-in token
verification remains enabled. Keycloak is not used.

The web port accepts ingress only from Traefik. The proxy checks Host, Origin and
Fetch Metadata before supplying its token. Cloudflare Tunnel rejects the entire
`*.internal.api-api-api.com` zone; External DNS does not publish this route.
Vault secret changes restart the deployment so both containers use the same token.

## State and configuration

State lives on the 5 GiB NFS PVC `hermes-data`. SQLite uses DELETE journaling
for NFS. Recreate updates keep a single gateway writer. The PVC is retained
when the application is pruned or deleted. This is persistent storage, not a backup.
Hermes runs on worker nodes, without a Kubernetes service-account token.

Provider login state is stored in `/opt/data/auth.json`; OAuth refreshes require
that file to remain writable. The initial recovery snapshot is in Vault
`kv/apps/hermes/oauth`, field `auth_json`. Refresh-token rotation can make that
snapshot stale; use a fresh login if recovery requires it. Never commit credentials.

Edit the ConfigMap in Git to change configuration. It is copied to the volume
at pod startup, and its checksum triggers a rollout. Agent changes to
config.yaml are overwritten on restart.

## Sign in again

For device-code authentication:

```sh
kubectl -n hermes exec -it deployment/hermes -- hermes auth add openai-codex
```

For browser authentication, forward the fixed OAuth callback port first:

```sh
kubectl -n hermes port-forward deployment/hermes 1455:1455
# In another terminal:
kubectl -n hermes exec -it deployment/hermes -- hermes auth add openai-codex --browser --no-browser
```

Open the printed OpenAI URL and finish sign-in. A separate login keeps the local
Codex session independent from Hermes.
