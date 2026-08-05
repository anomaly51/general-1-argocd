# Authentik

This wrapper deploys the official Authentik chart with one server, one worker,
and one persistent PostgreSQL instance. Argo CD discovers this directory
automatically through the infrastructure ApplicationSet.

The wrapper renders the ordinary Kubernetes Secret `authentik-credentials`
from the base64-encoded values in `values.yaml`. Authentik and PostgreSQL both
consume that Secret. Base64 is not encryption, so repository access also grants
access to these credentials.

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
Changing its value in Git later does not change the existing user's password.
