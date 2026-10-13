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
  Mailu is placed on `general-1-worker-2`, away from the small control-plane node.
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

ExternalDNS publishes the sender hostname `mail.api-api-api.com` (DNS only),
SPF, the Mailu-generated DKIM public key, and DMARC (`p=none` during setup) from
`templates/dns.yaml`. The first Mailu hostname is also its outbound SMTP identity.
The A record uses the observed WAN address `213.231.50.44`; update it if the ISP
changes this address. The DKIM private key stays in Mailu's persistent admin data;
regenerating it requires updating the public TXT record in Git.

Direct external delivery is still blocked on TCP/25. On 2026-10-13, Gmail and
iCloud port 25 timed out both from a Mailu pod and directly from its worker VM.
The same worker reached Gmail ports 465/587 and HTTPS. Proxmox had no cluster,
node or VM firewall rules for this VM; its NIC firewall was disabled. The worker's
output policy accepts traffic and has no SMTP-specific blocking rules.
The remaining network path is the Xiaomi router `192.168.1.1` and the ISP.
DNS records alone do not remove this transport restriction.

No public MX is advertised while inbound SMTP is unavailable. To use direct
SMTP, allow outbound TCP/25 and ask the IP owner to set PTR for `213.231.50.44`
to `mail.api-api-api.com` (currently an ISP pool hostname). If receiving mail is
needed, forward inbound TCP/25 to Mailu, enable its SMTP listener and publish MX.
Alternatively configure `externalRelay.host` and `externalRelay.existingSecret`
with a verified SMTP provider, store credentials in Vault, and adjust SPF for
that provider. Use `tls.outboundLevel: secure` for a verified STARTTLS relay.

References: [chart](https://github.com/Mailu/helm-charts/tree/mailu-2.8.0/charts/mailu),
[Mailu configuration](https://mailu.io/2024.06/configuration.html),
[account import](https://mailu.io/2024.06/cli.html#config-import).
