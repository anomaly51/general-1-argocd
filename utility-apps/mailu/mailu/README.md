# Mailu

Mailu 2024.06.58, upstream Helm chart 2.8.0. Argo CD manages this wrapper as
`mailu-mailu` in namespace `mailu`.

- Administration: https://mailu.internal.api-api-api.com/admin/ (LAN/VPN).
- SMTP: `smtp.internal.api-api-api.com:465`, implicit TLS and authentication.
- Mail domain: `api-api-api.com`; application sender: `keycloak@api-api-api.com`.
- Admin login: `admin@api-api-api.com`; password in Vault
  `kv/mailu/config`, field `initial-account-password`.
- SMTP credentials: Vault `kv/keycloak/smtp`; this account has no admin rights.
- Webmail, ClamAV, OLETools and optional fetching/indexing services are disabled.
- SQLite, Postfix, Dovecot and Rspamd have separate 1 GiB local volumes.
  Redis uses the existing operator and ephemeral storage for its cache/counters.
  This is a single-instance setup without automatic mail-data backups.
- Only Keycloak can reach the front SMTP listener; the internal gateway reaches
  the admin UI. Backend services and Redis are confined to the Mailu namespace.

The initial admin is created by the chart. Bootstrap the application SMTP account
once with Mailu's `flask mailu config-import --update`, supplying its password from
Vault; do not commit the password or use the admin account for applications.
Mailu stores account data on its persistent volume. Keycloak realm SMTP settings
are managed by [keycloak-terraform](https://github.com/anomaly51/keycloak-terraform).

## External delivery

Outbound TCP/25 probes to Gmail and iCloud timed out from an unrestricted cluster
workload on 2026-10-13. No external SMTP relay is configured yet. Local delivery
can work, but accepting a message into Mailu's queue does not demonstrate delivery
to an external mailbox. No public MX is advertised for this private installation.

To enable external delivery, configure the upstream chart's `externalRelay.host`
and `externalRelay.existingSecret` with a verified SMTP provider, store its
credentials in Vault, and publish the provider's required SPF/DKIM records.
Use `tls.outboundLevel: secure` when the relay supports verified STARTTLS.

References: [chart](https://github.com/Mailu/helm-charts/tree/mailu-2.8.0/charts/mailu),
[Mailu configuration](https://mailu.io/2024.06/configuration.html),
[account import](https://mailu.io/2024.06/cli.html#config-import).
