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

Workload-specific resources belong to the chart that consumes them. For
example, a workload that consumes Vault data owns its dedicated ServiceAccount,
`VaultAuth`, and `VaultStaticSecret`. The shared `VaultConnection/default` is
owned by the Vault Secrets Operator chart in `vault-system`.

Vault server policies and Kubernetes auth roles are documented under
`vault-config/`. VSO references those roles but does not create them.

Application directory names must be valid Kubernetes names and unique across
all namespace directories because they become Argo CD Application names.
