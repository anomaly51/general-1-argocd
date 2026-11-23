# Marstek dashboard

Public URL: https://marstek.api-api-api.com/

The existing Cloudflare Tunnel and Traefik Gateway send traffic to an unprivileged
NGINX pod in `general-1-k3s`. The pod serves the dashboard and its assets from
the Helm release. It proxies telemetry and SSE to the home-LAN HTTP endpoint
configured in `values.yaml`. The single read-only UDP collector in CT 104 on
machine-1 polls the station every 30 seconds.

## Publish the dashboard

Run `npm run build` in the dashboard source checkout. Replace the chart's
`frontend/` directory contents with the generated `static/index.html` and
`static/assets/` directory. Run `helm lint` on the chart, then commit the chart
with the new files and push to `main`. Argo CD's `networking-marstek` application
rolls out the pod when the frontend files change. The bundle must fit in a
1 MiB ConfigMap.

Check the page and its JavaScript after sync. Check `/api/status` for
`online`, `updated_at`, and `age_seconds` to verify station telemetry.
`/health` checks the upstream HTTP service. The pod's readiness probe checks
that the deployed dashboard can be served.
