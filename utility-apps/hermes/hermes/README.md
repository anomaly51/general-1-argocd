# Hermes Agent

One official Hermes v0.23.2 container with a small LAN proxy in namespace `hermes`, managed by Argo CD.
`templates/resources.yaml` contains the complete deployment, with the image pinned by digest.

Chat uses the ChatGPT/Codex subscription provider and `gpt-6.1-sol`, matching
the current local Codex model. Hermes has its own OAuth session.

Start a chat:

```sh
kubectl -n hermes exec -it deployment/hermes -- hermes
```

The internal API is `http://hermes.hermes.svc.cluster.local:8642/v1`.
Its bearer key is in Vault `kv/apps/hermes`, field `API_SERVER_KEY`.
Open **https://hermes.internal.api-api-api.com** from the LAN (or a VPN with
access to the LAN and its internal DNS). There is no browser login and no public
route. All devices with access to this internal endpoint share the same agent.

The internal Gateway terminates HTTPS. A small Nginx sidecar forwards a deployment
session token to Hermes on loopback; the token stays server-side in Vault
`kv/apps/hermes`, field `HERMES_DASHBOARD_SESSION_TOKEN`. Hermes' built-in token
verification remains enabled. Keycloak is not used.

The web port accepts ingress only from Traefik. The proxy checks Host, Origin and
Fetch Metadata before supplying its token. Cloudflare Tunnel rejects the entire
`*.internal.api-api-api.com` zone; External DNS does not publish this route.
Vault secret changes restart the deployment so both containers use the same token.

## Telegram

Chat with [@anomaly51_hermes_bot](https://t.me/anomaly51_hermes_bot).
The existing gateway polls Telegram; no webhook, public ingress, or separate bot
process is needed. Only the owner's numeric user ID in `TELEGRAM_ALLOWED_USERS`
is admitted. Other DMs are ignored; group access is disabled.

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS` are stored in Vault
`kv/apps/hermes` and injected by VSO. Keep one gateway replica with the Recreate
strategy so two instances never poll this bot simultaneously.

## Gmail and Google Calendar through MCP

`files/gmail_mcp.py` runs as a stdio child of Hermes, using the MCP, HTTP and
Google auth libraries already pinned in the Hermes image. No new Deployment,
public port, Chrome extension or always-on Mac is needed. Configuration:

```yaml
mcp_servers:
  gmail:
    command: /opt/hermes/.venv/bin/python
    args: [/opt/gmail/gmail_mcp.py]
```

The mail tools are `list_accounts`, `gmail_search`, `gmail_read`, `gmail_compose`
and `gmail_api`. Every mail operation requires an explicit account email.
Compose creates a draft unless `send: true` is supplied. The advanced API tool
supports labels, archive, trash, attachments, settings and permanent deletion;
it is confined to Google's Gmail endpoint and the authenticated `users/me`.
Mail content is treated as untrusted data. Writes require an owner request;
permanent deletion requires explicit confirmation. These instructions guide the
agent; the OAuth scope itself grants full mailbox access.

Google Cloud project: `anomaly51-hermes-mail` (Hermes Mail), with Gmail API.
The OAuth information/privacy page is `https://hermes-mail.api-api-api.com/`.
It is served by a separate static-only Nginx listener on 8081: only `/` exists;
there is no upstream to the agent. The dashboard remains LAN-only on its original
internal hostname. `files/mail-info.html` contains the page and privacy notice.
Use a Desktop OAuth client and `https://mail.google.com/` for full mail access.
Drive, Workspace administration and Chrome sessions are not shared.
Set the OAuth application's audience to External / In production for persistent
refresh tokens; Testing can expire them after seven days. The private, unverified
app can show Google's consent warning. Workspace policies can still block consent,
and a Google/Cloud Identity account without a Gmail mailbox cannot be connected.

Authorize each mailbox once on the Mac:

```sh
python3 scripts/authorize_gmail.py \
  --client-secret /private/path/client_secret.json \
  --email YOUR_ACCOUNT@gmail.com \
  --output /private/path/gmail_credentials.json
```

Open the printed URL in Chrome. The helper binds only `127.0.0.1:8765`, validates
OAuth state + PKCE and verifies the returned mailbox address before adding it to
the private JSON bundle. Repeating with another email appends that account.
The output is mode 0600 and contains refresh credentials, never email contents.

For Calendar, enable Google Calendar API in the same Google project and run the
same authorization command with `--calendar` for each of the three accounts.
This preserves Gmail access and adds `calendar.events` plus
`calendar.calendarlist.readonly`. Google scopes include deletion of events, but
the MCP exposes only listing, reading, creation and updates, not deletion or
calendar sharing/ACL management. Store the updated bundle in the same Vault field;
no additional process, secret mount, service or configuration is needed.

Calendar tools: `calendar_list_calendars`, `calendar_events`, `calendar_event`,
`calendar_create`, `calendar_update`. Every call names the account. The default
calendar is `primary`, timezone `Europe/Kyiv`; timed events need explicit UTC
offsets. List all pages before claiming a slot is free. Creation uses a stable
request ID to prevent duplicates on retries. Updates require a last-read etag
and use `If-Match`, preserve unspecified fields and notify guests. Attendee arrays
replace the old list, so retain other attendees when changing one guest or RSVP.
Recurring instances are expanded in searches; use an instance ID for a one-off
change. For optional Google Meet creation, set `create_meet: true`.

Examples in Telegram: “Show tomorrow's meetings across all three accounts” or
“In ACCOUNT, create a meeting tomorrow 14:00–14:30 Kyiv time titled NAME”.

Store the bundle in Vault `kv/apps/hermes`, field `GMAIL_CREDENTIALS_JSON`, and
the Desktop client JSON in `GMAIL_CLIENT_SECRET_JSON`. For example, with an
already authenticated Vault CLI:

```sh
vault kv patch kv/apps/hermes \
  GMAIL_CREDENTIALS_JSON=@/private/path/gmail_credentials.json \
  GMAIL_CLIENT_SECRET_JSON=@/private/path/client_secret.json
```

VSO projects only `GMAIL_CREDENTIALS_JSON` to
`/opt/gmail-secrets/credentials.json`. The MCP reads refresh credentials there;
short-lived access tokens remain in memory and refresh without a browser.
Remove temporary local credential files after confirming the Vault write.
No app secrets belong in Git or macOS Keychain. To disconnect, remove the account
from this bundle and revoke Hermes Mail in the account's Google security settings.

Verify from Telegram: “List my connected Gmail accounts”, then “Show unread
messages for ACCOUNT”. Setup checks should only read profiles/labels; they should
not send messages, mark mail read or change the mailbox.

## Interviews Tracker

The `interviews_tracker` MCP connection uses a dedicated, revocable
`tracker:read` credential from `kv/apps/hermes`, field
`INTERVIEWS_TRACKER_MCP_TOKEN`. **Prepare application** on the Tracker
Vacancies page calls the internal Hermes API using OpenAI. This API platform
loads only the Tracker MCP tools. Telegram continues to use its own tool policy.
The Tracker authenticates to the internal API using the Vault field
`APP_HERMES_DRAFT_KEY` in `kv/apps/interviews-tracker`.

## State and configuration

The optional [Windows PC setup](windows-pc/README.md) connects Computer Use
and Codex CLI over LAN SSH and adds a live view of the Windows user's primary
monitor in Hermes. Use the [handoff prompt](windows-pc/HANDOFF.md) on Windows.

State lives on the 5 GiB NFS PVC `hermes-data`. SQLite uses DELETE journaling
for NFS. Recreate updates keep a single gateway writer. The PVC is retained
when the application is pruned or deleted. This is persistent storage, not a backup.
Hermes runs on worker nodes, without a Kubernetes service-account token.

Provider login state is stored in `/opt/data/auth.json`; OAuth refreshes require
that file to remain writable. The initial recovery snapshot is in Vault
`kv/apps/hermes/oauth`, field `auth_json`. Refresh-token rotation can make that
snapshot stale; use a fresh login if recovery requires it. Never commit credentials.

Edit the ConfigMap in Git to change configuration. It is copied to the volume
at pod startup, and its checksum triggers a rollout. Agent changes to
config.yaml are overwritten on restart.

## Sign in again

For device-code authentication:

```sh
kubectl -n hermes exec -it deployment/hermes -- hermes auth add openai-codex
```

For browser authentication, forward the fixed OAuth callback port first:

```sh
kubectl -n hermes port-forward deployment/hermes 1455:1455
# In another terminal:
kubectl -n hermes exec -it deployment/hermes -- hermes auth add openai-codex --browser --no-browser
```

Open the printed OpenAI URL and finish sign-in. A separate login keeps the local
Codex session independent from Hermes.
