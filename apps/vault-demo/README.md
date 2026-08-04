# Vault demo

This cluster-internal application demonstrates the complete secret delivery
path:

```text
Vault kv/apps/vault-demo
  -> Vault policy and Kubernetes auth role
  -> VaultAuth/vault-demo
  -> VaultStaticSecret/vault-demo
  -> Secret/vault-demo
  -> read-only /www/message.txt volume
  -> BusyBox HTTP server
```

The value is intentionally non-sensitive and the Service is `ClusterIP` only.
After Argo CD reports the application healthy, inspect it locally:

```sh
kubectl --kubeconfig ~/.kube/configs/general-1-k3s.yaml \
  -n apps port-forward service/vault-demo 8080:80
```

Then open <http://127.0.0.1:8080/message.txt>.

Only the disposable `message` key is exposed by this teaching application.
Never use it to display a real credential.
