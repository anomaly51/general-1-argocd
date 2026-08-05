# Kubernetes Secret demo

This application keeps the base64-encoded value directly in
`values.yaml`. The chart renders an ordinary Kubernetes
`Secret/secret-demo`, and the Deployment mounts its `message` key as
`/www/index.html`.

```text
Git values.yaml
      ↓ Helm
Kubernetes Secret/secret-demo
      ↓ volume
Pod /www/index.html
```

Encode a new value:

```sh
printf %s 'Hello from Kubernetes Secret' | base64
```

Put the result in `secret.data.message` and commit it. Base64 is encoding,
not encryption: anyone who can read the repository can decode the value.

To see the result:

```sh
kubectl --kubeconfig ~/.kube/configs/general-1-k3s.yaml \
  -n apps port-forward deployment/secret-demo 8080:8080
```

Open <http://127.0.0.1:8080/>.
