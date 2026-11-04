# Marstek public monitor

Public URL: https://marstek.api-api-api.com/

Cloudflare DNS is reconciled from the HTTPRoute by ExternalDNS. Traffic uses
the existing Cloudflare Tunnel and public Traefik Gateway, then a small unprivileged
NGINX proxy to `192.168.1.84:8080` in the home LAN (16 MiB memory request,
64 MiB limit). The existing Argo CD configuration excludes external
EndpointSlices; the proxy avoids changing cluster-wide reconciliation rules. Only GET requests to the listed page, assets,
status JSON and process health endpoint are routed.

The single collector remains in unprivileged Proxmox CT 104 (`marstek-dashboard`)
on machine-1, under OpenRC service `marstek`. It polls Venus E 3.0 over LAN UDP
30000 once per minute; public requests only read its cache. The station UDP
API, Proxmox and Home Assistant are not exposed by this route.

The upstream currently uses DHCP, MAC `BC:24:11:3B:39:0A`. Reserve its address
on the router to keep it stable; if the lease changes, update `upstream.address`
in values.yaml through GitOps. The station's own DHCP address is separately
configured in the collector. Do not start a second UDP collector.

`/health` reports only HTTP process health. Check `/api/status` for `online`,
`updated_at`, `age_seconds` and `error` to verify live station telemetry. The
charge/discharge power and ETA can be estimates, as labelled by the page.

Source and LAN deployment notes:
`/Users/nekoneki/Documents/Codex/2026-09-21/home-assistant-8-9-10-8/marstek-dashboard`.
No credentials are required or included in this chart.
