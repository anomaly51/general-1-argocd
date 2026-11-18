# Hermes Agent

One official Hermes v0.23.2 container, managed by Argo CD in namespace `hermes`.
`templates/resources.yaml` contains the complete deployment. The image is pinned by digest.

Open the web app locally:

```sh
kubectl -n hermes port-forward deployment/hermes 9120:9120
```

Then open http://localhost:9120. Kubernetes access is required; there is no public ingress.

CLI:

```sh
kubectl -n hermes exec -it deployment/hermes -- hermes
```

The internal API is `http://hermes.hermes.svc.cluster.local:8642/v1`.
Its bearer key is in Vault `kv/apps/hermes`, field `API_SERVER_KEY`.

State lives on the 5 GiB NFS PVC `hermes-data`. SQLite uses DELETE journaling
for NFS. Recreate updates keep a single gateway writer. The PVC is retained
when the application is pruned or deleted. This is persistent storage, not a backup.

Provider setup is pending selection. Provider login state is stored in
`/opt/data/auth.json`; OAuth refreshes require that file to remain writable.
Save a recovery copy to Vault after authentication. Never commit credentials.

Edit the ConfigMap in Git to change configuration. It is copied to the persistent
volume at pod startup. Agent changes to config.yaml are overwritten on restart.
