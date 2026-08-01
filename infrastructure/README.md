# Infrastructure applications

Infrastructure is organized by destination namespace and then by application:

```text
infrastructure/
└── <namespace>/
    └── <application>/
        ├── Chart.yaml
        ├── Chart.lock        # when the chart has dependencies
        ├── values.yaml
        └── templates/        # optional
```

`cluster/applicationsets/infrastructure.yaml` discovers every directory matching
`infrastructure/*/*`. The namespace comes from the first directory below
`infrastructure`, while the Argo CD Application and Helm release names normally
come from the application directory.

To add an application, add its Helm chart under the target namespace. Do not
edit the ApplicationSet and do not add a per-application Argo CD metadata file.

Application directory names must be valid Kubernetes names and unique across
all namespace directories because they become Argo CD Application names.
