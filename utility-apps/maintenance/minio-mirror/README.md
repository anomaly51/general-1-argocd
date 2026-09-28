# MinIO image mirror

The existing MinIO release is cached on general-1-worker-1, but anonymous pulls
from both external registries fail with HTTP 401. This one-time Job exports only
the cached linux/amd64 manifest and its checksum-verified image blobs to a pod
temporary volume, then imports them into Harbor. It never reads running
container files, MinIO buckets, database data, or application credentials.

Source index: sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e

Preserved amd64 manifest: sha256:3f97c5651cb6662b880c787a232b6b34fec8d8922e08d6617b25d241a21164bb

Destination: harbor.internal.api-api-api.com/applications/minio:RELEASE.2025-04-22T22-12-26Z

Vault Kubernetes role maintenance-minio-mirror is bound only to the minio-mirror
service account in maintenance. Its policy reads kv/data/ci/harbor-applications
and permits lookup/renew/revoke of its own token. The Job uses the project-scoped
Harbor robot via a mounted Secret and verifies the remote manifest digest.
No production values are changed by this maintenance operation.
