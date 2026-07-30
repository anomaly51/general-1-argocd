# Infrastructure

Reusable cluster infrastructure manifests live here.

Create one directory per component, for example:

```text
infrastructure/
  cert-manager/
  external-secrets/
  ingress-nginx/
```

Expose components to Argo CD from `cluster/` with an `Application`
or `ApplicationSet`.
