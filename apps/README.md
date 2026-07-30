# Apps

Reusable workload manifests live here.

Create one directory per application, for example:

```text
apps/
  api/
  worker/
  frontend/
```

Expose applications to Argo CD from `cluster/` with an `Application`
or `ApplicationSet`.
