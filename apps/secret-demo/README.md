# Direct value demo

This application keeps the `MESSAGE` literal directly in
`templates/deployment.yaml`. BusyBox writes the value to `/www/index.html`
before starting the HTTP server.

```text
Deployment template
      ↓ Helm
Pod environment variable literal
      ↓ startup command
Pod /www/index.html
```

Edit the `MESSAGE` entry in `templates/deployment.yaml` to change the page.
This direct-value pattern is temporary: do not treat the repository as a safe
place for credentials.

To see the result:

```sh
kubectl --kubeconfig ~/.kube/configs/general-1-k3s.yaml \
  -n apps port-forward deployment/secret-demo 8080:8080
```

Open <http://127.0.0.1:8080/>.
