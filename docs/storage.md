# Shared NFS storage

## Object model

```text
NFS server
  -> NFS provisioner
  -> cluster-wide StorageClass
  -> namespaced PVC
  -> dynamically created PV
  -> one or more Pods
```

The provisioner creates a separate NFS directory and PV for every PVC. It is
not in the data path after the volume is bound: each node mounts the NFS export
directly.

Use one PVC per independent dataset. Multiple Pods in the same namespace can
mount the same PVC when they need shared files. PVCs cannot be referenced
across namespaces, so create a claim in each namespace instead.

## Create a volume

Do not create a PV manually. Commit a PVC with the application:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-app-data
spec:
  storageClassName: nfs-client
  accessModes:
    - ReadWriteMany
  volumeMode: Filesystem
  resources:
    requests:
      storage: 1Gi
```

Mount it in a Deployment or StatefulSet:

```yaml
spec:
  template:
    spec:
      containers:
        - name: app
          volumeMounts:
            - name: data
              mountPath: /var/lib/my-app
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: my-app-data
```

Use `ReadWriteMany` when Pods on different nodes share the claim. A Pod that
only needs its own claim may request `ReadWriteOnce`, but that access mode is
not an application-level permission boundary.

## Lifecycle

- `nfs-client` is not the default StorageClass; applications must select it
  explicitly.
- Dynamically created PVs use `Retain`. Deleting a PVC leaves its PV in
  `Released` state and preserves the NFS directory.
- Recovery and final deletion of a retained PV are administrator operations.
- The default `local-path` class remains available for disposable,
  node-local storage.

## Operational limits

The requested PVC size is Kubernetes metadata, not an NFS quota. One workload
can still fill the whole export, and this provisioner does not support volume
resize. Capacity limits, free-space and inode monitoring, backups, export
allowlists, and NFS server availability must be managed on the NFS server.

The export should only allow the four trusted General-1 node addresses.
Applications sharing this export are in the same storage trust zone.

## Inspect storage

```bash
kubectl get storageclass
kubectl get pvc --all-namespaces
kubectl get pv
```
