# General-1 GitOps

Argo CD discovers applications from the directory tree; there is no central
application list.

```text
cluster/                         Argo CD projects and ApplicationSets
infrastructure/<namespace>/<app> cluster services, one Helm chart per app
apps/<app>                       ordinary workloads in namespace apps
```

Adding a directory under `infrastructure/<namespace>/` or `apps/` is enough for
the corresponding ApplicationSet to create an Argo CD Application.

## Adding a Kubernetes Secret

Keep an ordinary Secret next to the workload that consumes it:

```text
infrastructure/monitoring/influxdb/
├── templates/
│   └── secret.yaml
├── Chart.yaml
└── values.yaml
```

Store base64-encoded values under `secret.data` in `values.yaml`, render
them as a core Kubernetes `Secret` from `templates/secret.yaml`, and point
the workload at that Secret. The examples in Grafana, Authentik, PVE exporter,
and `apps/secret-demo` all use this pattern.

Base64 is only an encoding and does not protect the value. Anyone who can read
the Git repository can decode every committed Secret.
