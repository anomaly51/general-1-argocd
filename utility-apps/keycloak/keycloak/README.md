# Keycloak

Official namespace-scoped Keycloak Operator 26.8.0, reconciled by Argo CD.
The operator installation is pinned in `../../../cluster/operators/keycloak.yaml`.
This minimal Helm chart contains the General-1 runtime configuration.
The `utility-apps` ApplicationSet manages it as `keycloak-keycloak` in the
`keycloak` namespace.

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

Installation verified on 2026-10-11: OIDC authorization code flow with PKCE,
public administration blocked, both Prometheus targets healthy, and service
available during an operator rollout. Backup `20261011T003228` was restored
into an isolated PostgreSQL cluster; schema, administrator and required MFA
matched the live database. The temporary verification resources were removed.
Standby backups may need to wait for the final WAL segment to be archived;
the standard archive timeout is five minutes.
