# Authentik

This wrapper deploys the official Authentik chart with one server, one worker,
and one persistent PostgreSQL instance. Argo CD discovers this directory
automatically through the infrastructure ApplicationSet.

Vault Secrets Operator reads `kv/authentik/authentik` and creates the
`authentik-secrets` Kubernetes Secret used by Authentik and PostgreSQL. Secret
values are not stored in Git.

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
Changing that Vault value later does not change the existing user's password.
