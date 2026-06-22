# skchat media plane — v2 Swarm stacks (B4)

Declarative Docker Swarm stack definitions for the **shared media plane**: the
LiveKit SFU and the coturn TURN/STUN relay. These make the media plane a
reproducible v2 stack instead of the ad-hoc host launchers that run it today.

| File | Service | What it exposes |
|---|---|---|
| `livekit-stack.yml` | LiveKit SFU | `7880/tcp` (HTTP API + WebSocket signalling), `7881/tcp` (ICE/TCP), `50000–50200/udp` (RTC media). Bound on the host's **tailnet IP** `100.108.59.57` only. |
| `coturn-stack.yml` | coturn TURN/STUN | `3478/udp+tcp` (STUN/TURN control), `5349/udp+tcp` (TURN/TLS, opt-in), `49152–65535/udp` (relay allocation pool). Host networking. |

Both run under **`network_mode: host`** (LiveKit + coturn both need direct
access to the host UDP stack — Swarm's mesh cannot proxy large UDP ranges).
Default posture is **tailnet-first**: nothing binds `0.0.0.0` for public reach;
off-tailnet access goes through the existing `tailscale serve`/funnel proxies
(see "Relationship to today's proxies" below).

---

## Secrets and configs they expect (for B3)

Nothing sensitive is inlined in these YAMLs. They reference externally-supplied
material:

### LiveKit
- **Swarm config object `livekit_config`** (external) — the rendered
  `livekit.yaml`. Its `keys:` block (API key → secret pairs, today
  `skchat-opus` / `skchat-lumina`) is the **SECRET** part and lives only in this
  config object, created from a host file populated from OpenBao.
  ```bash
  docker config create livekit_config /var/data/deploy_livekit/livekit.yaml
  ```
  Rotate: `docker config create livekit_config_v2 ...`, point the stack's
  `configs:` at it, redeploy.
- Env file `/var/data/deploy_livekit/livekit.env` (chmod 600) — `LIVEKIT_KEYS`,
  `LIVEKIT_REDIS_ADDRESS` (optional). Template: `deploy/v2/media-plane.env.example`.

### coturn
Env file `/var/data/deploy_coturn/coturn.env` (chmod 600):

| Var | Secret? | Notes |
|---|---|---|
| `SKCHAT_TURN_SECRET` | **YES (P0)** | `use-auth-secret` static secret. MUST equal `SKCHAT_TURN_SECRET` in `skchat.env` (skchat derives short-lived HMAC-SHA1 creds from it). |
| `SKCHAT_TURN_REALM` | no | e.g. `noroc2027.tail204f0c.ts.net`. |
| `COTURN_EXTERNAL_IP` | no | The host's **publicly reachable** IP. Required so relays advertise the right candidate to off-tailnet guests. |
| `COTURN_MIN_PORT` / `COTURN_MAX_PORT` | no | Relay UDP range (default `49152`/`65535`). Must match host firewall. |

OpenBao paths and the full secret inventory: see `deploy/SECRETS.md`
(§Media plane, §`SKCHAT_LIVEKIT_API_KEY/SECRET`, §`SKCHAT_TURN_SECRET`).

> **NEVER** commit a filled `.env`, a real `livekit.yaml` with live `keys:`, or
> the coturn static secret. (The host launcher's `start-coturn.sh` reads the
> secret from `~/.skchat/coturn/coturn.secret`, kept out of git.)

---

## Deploy

Both stacks share the `media` overlay namespace and deploy into one stack name.
**Deploy LiveKit first** — it creates the `media` network that `coturn-stack.yml`
references as external (or pre-create it:
`docker network create --driver overlay --attachable media`).

```bash
# 1. LiveKit (creates the `media` overlay + the livekit_config config object)
docker config create livekit_config /var/data/deploy_livekit/livekit.yaml
set -a && source /var/data/deploy_livekit/livekit.env && set +a
docker stack deploy --env-file /var/data/deploy_livekit/livekit.env \
  -c deploy/stacks/livekit-stack.yml media

# 2. coturn
set -a && source /var/data/deploy_coturn/coturn.env && set +a
docker stack deploy --env-file /var/data/deploy_coturn/coturn.env \
  -c deploy/stacks/coturn-stack.yml media
```

Node placement labels (pin both to the tailnet/firewall host, today `noroc2027`):
```bash
docker node update --label-add livekit=true <node>
docker node update --label-add coturn=true  <node>
```

Validate without deploying:
```bash
docker stack config -c deploy/stacks/livekit-stack.yml   # needs LIVEKIT_KEYS set
docker stack config -c deploy/stacks/coturn-stack.yml    # needs the coturn env vars set
```

---

## How these map to today's running LiveKit + coturn

These stacks are reconciled with the **actual** live config, not a generic example:

### LiveKit (today: `livekit-server.service` user unit)
- ExecStart: `~/.local/bin/livekit-server --config ~/.config/livekit/livekit.yaml`
- `livekit.yaml`: `port: 7880`, `bind_addresses: [100.108.59.57]`,
  `rtc.tcp_port: 7881`, `rtc.port_range_start/end: 50000/50200`,
  `use_external_ip: false`, `keys: {skchat-opus, skchat-lumina}`, `log_level: info`.
- The stack reproduces this 1:1: same image-config model (config-file driven, no
  `LIVEKIT_KEYS` env needed — the `keys:` live in the mounted `livekit.yaml`
  config object), same tailnet bind IP, same RTC range, host networking.
- Healthcheck hits `http://100.108.59.57:7880/healthz` (the live bind).

### coturn (today: `skchat-coturn.service` → `start-coturn.sh`)
- `docker run --network host coturn/coturn:4.6` with realm
  `noroc2027.tail204f0c.ts.net`, `--use-auth-secret`, static secret from
  `~/.skchat/coturn/coturn.secret`, `--listening-port 3478`,
  `--min-port 49152 --max-port 65535`, the full `--denied-peer-ip` SSRF block,
  `--cipher-list HIGH --no-tlsv1 --no-tlsv1_1 --fingerprint --stale-nonce=0`.
- The stack reproduces all of those flags, sourcing the secret as
  `${SKCHAT_TURN_SECRET}` instead of inlining it, and **adds `--external-ip`**
  (the launcher omitted it; needed for correct candidate advertisement to
  off-tailnet guests). TURN URL today: `turn:100.108.59.57:3478`.

> **Cutover note:** these are authored but **NOT deployed**. To switch from the
> host launchers to the stacks, stop the user units first to free the ports/IP:
> ```bash
> systemctl --user stop livekit-server.service
> systemctl --user disable --now skchat-coturn.service   # or: docker rm -f skchat-coturn
> ```
> then run the deploy commands above. Do this only when intending to cut over —
> running both would collide on `100.108.59.57:7880` / `:3478`.

---

## Relationship to today's tailscale-serve / funnel proxies (don't double-expose)

The SFU is reached off-host through `tailscale serve` / funnel — these stacks do
**not** add their own public bind, so there's no double-exposure. Current proxy
map (from `tailscale serve status`), all pointing at the SFU on
`100.108.59.57:7880`:

| Proxy entry | Target | Reach |
|---|---|---|
| `https://noroc2027.tail204f0c.ts.net/livekit-ws` | `100.108.59.57:7880` | Funnel (public) |
| `https://noroc2027.tail204f0c.ts.net:8443/` | `100.108.59.57:7880` | tailnet only |
| `https://noroc2027.tail204f0c.ts.net:10001/` | `100.108.59.57:7880` | Funnel (public) |
| `https://noroc2027.tail204f0c.ts.net:10000/{app,api,conf,join,livekit,guest/join,daemon}` | webui :8765 / daemon :9385 | Funnel (public) — app + guest entry |

Because the stack keeps the SFU bound to `100.108.59.57:7880` (the address every
proxy rule already targets), **the existing serve/funnel rules keep working
unchanged** after cutover — no proxy edits needed.

- **LiveKit:** tailnet clients use `wss://noroc2027.tail204f0c.ts.net:8443`;
  public guests use the funnel `:10001` / `/livekit-ws`. The stack adds nothing
  public itself.
- **coturn:** TURN is UDP and is **not** behind `tailscale serve` (which proxies
  TCP/HTTP). Off-tailnet guests reach coturn directly at the host's public IP
  (`COTURN_EXTERNAL_IP`) on `3478` + the relay range — so the **host firewall**
  is what gates it, not the funnel. Tailnet peers never touch coturn (tier-1
  Tailscale / tier-2 LAN). Do not try to front coturn with `tailscale serve`.

### ICE ladder (where coturn sits)
1. **Tailscale** — both peers on the tailnet (no relay). Default for agents.
2. **LAN / same subnet** — no relay.
3. **coturn TURN relay** — only for off-tailnet guests (funnel/CF link, mobile
   on carrier NAT). ← `coturn-stack.yml`
4. skmesh / Netbird — future.
