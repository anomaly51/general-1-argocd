# Authentik

This chart deploys one Authentik server, one worker, and one persistent
PostgreSQL instance. Argo CD discovers this directory automatically through the
infrastructure ApplicationSet.

Credentials are temporarily embedded as plain strings directly in
`templates/workloads.yaml` and `templates/database.yaml`. They render as
literal container environment values; there is no `values.yaml` indirection and
no Kubernetes Secret.

Repository access grants immediate access to every credential in this chart.

Access the cluster-internal service locally:

```sh
kubectl --kubeconfig ~/.kube/configs/general-1-k3s.yaml \
  -n authentik port-forward service/authentik-server 9000:80
```

Open <http://127.0.0.1:9000/> and sign in as `akadmin`. The bootstrap password
is stored in macOS Keychain:

```sh
security find-generic-password -w \
  -s general-1-authentik-akadmin-password \
  -a akadmin
```

`AUTHENTIK_BOOTSTRAP_PASSWORD` is consumed only during the first startup.
Changing it later does not change the existing user's password. Likewise,
changing the PostgreSQL value in Git does not rotate the password stored in an
existing database; both sides must be migrated together.
