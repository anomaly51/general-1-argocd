# Keycloak

Official namespace-scoped Keycloak Operator 26.8.0, reconciled by Argo CD.
The operator installation is pinned in `../operators/keycloak.yaml`.
This directory contains only the General-1 runtime configuration.

- Public issuer base: https://keycloak-general1.api-api-api.com
- Private administration: https://keycloak.internal.api-api-api.com/admin/
- Credentials: Vault `kv/keycloak/admin`; first login requires OTP enrollment.
- Two Keycloak instances; PostgreSQL 17 on three workers through CloudNativePG.
- HTTPS to Keycloak; PostgreSQL TLS with server certificate verification.
- Public routing allows only `/realms`, `/resources`, and `/.well-known`.
- Private hostname is excluded from Cloudflare Tunnel; LAN/VPN is required.
- Daily PostgreSQL backup at 02:00 UTC plus continuous WAL archiving to the
  private MinIO bucket `general1-keycloak-backups`, with seven-day retention.
- Keycloak metrics are scraped by the existing standalone Prometheus.

Versions change only through Git. Before upgrading Keycloak, take a database
backup and read the migration notes: reverting an image does not revert a
database migration. CRDs, the database, and its volumes are protected from
automatic Argo CD pruning. MinIO data is on the existing NFS server; this is
not an off-site backup. General-1 currently has one control-plane node.

Bootstrap credentials are temporary and are retired after permanent admin
creation. Application realms/clients are added when an application is connected.
