# Hermes Agent

One official Hermes v0.23.2 container in namespace `hermes`, managed by Argo CD.
`templates/resources.yaml` contains the complete deployment, with the image pinned by digest.

Chat uses the ChatGPT/Codex subscription provider and `gpt-6.1-sol`, matching
the current local Codex model. Hermes has its own OAuth session.

Start a chat:

```sh
kubectl -n hermes exec -it deployment/hermes -- hermes
```

The internal API is `http://hermes.hermes.svc.cluster.local:8642/v1`.
Its bearer key is in Vault `kv/apps/hermes`, field `API_SERVER_KEY`.
There is no public ingress or messaging integration in this initial setup.

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
