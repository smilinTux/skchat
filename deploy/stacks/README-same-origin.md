# skchat comms-suite — same-origin Swarm stack (B2 + B3 + B5)

One Traefik origin fronts the Flutter web app **and** every API the browser app
calls, so the app never makes a cross-origin request → **no CORS**.

- Stack: [`skchat-stack.yml`](skchat-stack.yml)
- Secrets: [`secrets.example`](secrets.example) + [`make-secrets.sh`](make-secrets.sh)
- Backup/restore: [`backup.md`](backup.md)

Distinct from the older monolith stack in `deploy/v2/` (which routes everything
through the single `webui:8765` FastAPI). This stack splits the surface into a
static Flutter front-end + separate `skcomms-api` and `sk-access` services, then
re-unifies them at the edge under one host via Traefik path routing.

---

## Same-origin routing (the B2 goal)

Traefik runs inside the stack on host `:8081` and routes by path prefix:

| Path | Upstream service | Container port | Prefix stripped? |
|------|------------------|----------------|------------------|
| `/` | `flutter-web` (B1 `skchat-webui:v2`) | 80 | n/a (priority 1, catch-all) |
| `/api` | `skcomms-api` (B1 `skchat-daemon:v2`, uvicorn `skcomms.api:app`) | 9384 | yes → `/` |
| `/access` | `sk-access` (B1 `skchat-webui:v2`, uvicorn `skchat.access:app`) | 9386 | yes → `/` |
| `/tool` | `sk-access` (same service) | 9386 | yes → `/` |
| `/livekit-ws` | B4 `livekit` SFU over the shared `media` overlay | 7880 | (route in B4 / see below) |

API routers run at Traefik `priority: 10`; the Flutter catch-all is `priority: 1`,
so `/api`, `/access`, `/tool` win and only unmatched paths fall through to the app.

### Test URL it yields (tailnet-first)

After `tailscale serve --bg https+insecure://localhost:8081`:

```
https://noroc2027.tail204f0c.ts.net/            ← Flutter web app
https://noroc2027.tail204f0c.ts.net/api/...     ← skcomms-api  (same-origin, no CORS)
https://noroc2027.tail204f0c.ts.net/access/...  ← sk-access    (same-origin)
https://noroc2027.tail204f0c.ts.net/tool/...    ← sk-access    (same-origin)
wss://noroc2027.tail204f0c.ts.net/livekit-ws    ← LiveKit WS   (same-origin)
```

The Flutter build must call **relative** URLs (`/api`, `/access`, `/tool`,
`/livekit-ws`) so it inherits the page origin. With that, the CORS workaround in
`src/skchat/daemon.py:877` (`Access-Control-Allow-Origin: *`) becomes unnecessary.

---

## Ingress

This is a **tailnet-first** stack. It binds exactly one host port (`:8081`,
Traefik). It does **not** open a new public port by itself.

### Tailnet (default)

```bash
tailscale serve --bg https+insecure://localhost:8081
# → https://noroc2027.tail204f0c.ts.net  (the single origin, WireGuard-encrypted)
```

### Public (opt-in — only what is ALREADY public today)

The current Funnel (`tailscale funnel status`) already fronts the comms surface
on `:10000` and root, pointing at the old `:8765` monolith. To migrate to the
same-origin stack, re-point those Funnel rules at Traefik `:8081`:

```bash
# Replace the old per-path :8765 rules with a single root proxy to Traefik.
# (Traefik now owns the /api, /access, /tool, /livekit-ws routing internally.)
tailscale funnel --bg 8081      # or: tailscale serve --bg --funnel ... → :8081
```

Do this under supervision so the public surface is not interrupted; nothing in
the stack toggles Funnel automatically.

### Cluster-edge (alternative — *.douno.it)

To publish on the shared cluster Traefik (the `cloud-public-prod` overlay used by
`deploy/v2/`) instead of the in-stack Traefik, attach `flutter-web` / the API
services to `cloud-public-prod` and add `Host(...)` rules. Not the default here —
this stack is intentionally self-contained and tailnet-first.

---

## `/livekit-ws` and the `media` overlay

Traefik joins the external `media` overlay so it can route `/livekit-ws` to the
B4 `livekit` SFU. Because B4's LiveKit uses `network_mode: host` (see
`deploy/v2/livekit-stack.yml`), the cleanest wiring is one of:

1. **Funnel/serve passthrough** (matches today): keep LiveKit WS on its existing
   tailnet/funnel port and have the app use that URL via `SKCHAT_LIVEKIT_URL`; OR
2. **Traefik → host SFU**: add a Traefik `ExternalName`/host route to
   `livekit:7880` once B4 also publishes the SFU on the `media` overlay (not just
   host networking). Until B4 exposes :7880 on `media`, prefer option 1 and set
   `SKCHAT_LIVEKIT_URL` to the existing `wss://...:8443` tailnet URL.

The stack leaves `/livekit-ws` as a same-origin *intent*; the concrete upstream
is finalised with B4 (the secrets `livekit_api_key`/`livekit_api_secret` here MUST
match B4's `LIVEKIT_KEYS`).

---

## Image-port / entrypoint adjustments

These depend on exactly what the B1 images expose; adjust if B1 differs:

- **`flutter-web`** — assumes `skchat-webui:v2` serves the static Flutter build on
  container `:80`. If it serves on `:8765`, change
  `traefik.http.services.skchat-web.loadbalancer.server.port` to `8765` and the
  healthcheck port to match (**§Image-port**).
- **`sk-access` entrypoint** — assumes `uvicorn skchat.access:app` on `:9386`.
  If the B1 image ships a different console script (e.g. `skchat-access`), swap
  the `command:` (**§sk-access-entrypoint**).
- **`skcomms-api` entrypoint** — `uvicorn skcomms.api:app` on `:9384`, per
  `runbooks/live-adapter-test.md:318`.

---

## Supervised deploy steps (do NOT run unsupervised)

```bash
# 1. Networks (idempotent)
docker network create --driver overlay --attachable skchat-app || true
docker network create --driver overlay --attachable media       || true   # shared w/ B4

# 2. Secrets (B3) — fill the gitignored env first
cp deploy/stacks/secrets.example deploy/stacks/.env.secrets
$EDITOR deploy/stacks/.env.secrets            # values; MUST match B4 livekit/turn
chmod 600 deploy/stacks/.env.secrets
deploy/stacks/make-secrets.sh
docker secret ls | grep -E 'livekit_api_key|livekit_api_secret|turn_secret|skmemory_pg_password|voice_fallback_url'

# 3. Validate (no deploy)
docker stack config -c deploy/stacks/skchat-stack.yml > /dev/null && echo "VALID"

# 4. Deploy (env-file carries NON-secret tuning only)
set -a && source /var/data/deploy_skchat/skchat.env && set +a
docker stack deploy --env-file /var/data/deploy_skchat/skchat.env \
  -c deploy/stacks/skchat-stack.yml skchat

# 5. Ingress
tailscale serve --bg https+insecure://localhost:8081

# 6. Verify
docker stack ps skchat
curl -fs http://localhost:8081/            # Flutter app (via Traefik)
curl -fs http://localhost:8081/api/health  # skcomms-api same-origin
curl -fs http://localhost:8081/access/health
# → browser: https://noroc2027.tail204f0c.ts.net/
```

### Teardown

```bash
docker stack rm skchat
# Volumes persist; wipe only to reset state:
# docker volume rm skchat_skchat-data skchat_skchat-outbox skchat_skchat-identity \
#                   skchat_skchat-skcapstone skchat_skchat-recordings
```
