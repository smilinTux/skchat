# skchat Secret Contract (Batch B3)

**Rule zero: no secret may be baked into a Docker image or committed to the
stack YAML.  All secrets are injected at runtime from the host `.env` file or
pulled from OpenBao.**

---

## 1. Host `.env` layout

File path on every deployment host:

```
/var/data/deploy_skchat/skchat.env
```

Permissions: `chmod 600 /var/data/deploy_skchat/skchat.env`
Owner: the user that runs `docker stack deploy` (typically `root` or a dedicated
`deploy` service account).

The file is loaded with:

```bash
docker stack deploy \
  --env-file /var/data/deploy_skchat/skchat.env \
  -c deploy/v2/skchat-stack.yml \
  skchat
```

A sanitized template (variable names, no values) lives at
`deploy/v2/skchat-prod.env.example`.  **Never** commit a filled-in `.env` file.

---

## 2. Secret inventory

| Variable | Purpose | Tier |
|---|---|---|
| `SKCHAT_LIVEKIT_API_KEY` | LiveKit SFU API key (JWT signer) | P0 — required |
| `SKCHAT_LIVEKIT_API_SECRET` | LiveKit SFU API secret (JWT signer) | P0 — required |
| `SKCHAT_TURN_SECRET` | coturn `use-auth-secret` static secret; derives ephemeral TURN credentials | P0 — required |
| `SKMEMORY_PG_PASSWORD` | skmem-pg Postgres password | P0 — required |
| `SKCHAT_LLM_URL` | LLM proxy endpoint (may embed auth token in URL) | P0 — required |
| `SKVOICE_FALLBACK_URL` | Fallback LLM endpoint (may embed auth token in URL) | P1 |
| `SKMEMORY_PG_USER` | Postgres username | P1 |
| `SKCHAT_IDENTITY` | CapAuth URI override (only needed to override SKAGENT resolution) | P2 — optional |

### What is NOT a secret (safe in stack YAML)

- `SKCHAT_LIVEKIT_URL` — the tailnet wss:// address of the LiveKit SFU (no credentials)
- `SKCHAT_TURN_URLS` / `SKCHAT_STUN_URLS` — TURN/STUN server addresses (no credentials; creds are derived at runtime)
- `SKCHAT_LLM_MODEL`, `SKVOICE_TTS_VOICE`, `SKAGENT`, daemon tuning knobs
- `SKMEMORY_PG_HOST`, `SKMEMORY_PG_PORT`, `SKMEMORY_PG_DB`

---

## 3. OpenBao path mapping

Migrating from host `.env` to OpenBao KV v2.  Target path layout:

```
secret/data/skchat/<env>/<key>
```

Where `<env>` is `prod`, `staging`, or `dev`.

| Secret | OpenBao path |
|---|---|
| `SKCHAT_LIVEKIT_API_KEY` | `secret/data/skchat/prod/livekit_api_key` |
| `SKCHAT_LIVEKIT_API_SECRET` | `secret/data/skchat/prod/livekit_api_secret` |
| `SKCHAT_TURN_SECRET` | `secret/data/skchat/prod/turn_secret` |
| `SKMEMORY_PG_PASSWORD` | `secret/data/skchat/prod/pg_password` |
| `SKMEMORY_PG_USER` | `secret/data/skchat/prod/pg_user` |
| `SKCHAT_LLM_URL` (if token-bearing) | `secret/data/skchat/prod/llm_url` |
| `SKVOICE_FALLBACK_URL` (if token-bearing) | `secret/data/skchat/prod/voice_fallback_url` |

Retrieval pattern (agent sidecar or pre-deploy hook):

```bash
# example: inject into the env file at deploy time
LIVEKIT_KEY=$(bao kv get -field=livekit_api_key secret/skchat/prod)
LIVEKIT_SECRET=$(bao kv get -field=livekit_api_secret secret/skchat/prod)
# ... write to /var/data/deploy_skchat/skchat.env
```

A Vault/OpenBao agent sidecar (Batch B3+ / infra-hardening) is the recommended
long-term pattern — the sidecar writes a `skchat.env` to a tmpfs mount, refreshes
on lease expiry, and signals the stack services to reload.  Until then the manual
inject-at-deploy approach is acceptable.

---

## 4. LiveKit API key / secret

- Issued by the skstack LiveKit instance (`livekit.skstack01.douno.it` or tailnet).
- The webui uses them to mint short-lived JWTs for browser/agent clients.  The
  JWTs are what clients use; the raw key+secret never leave the server.
- Rotate: generate a new keypair in the LiveKit config, update OpenBao, redeploy.
- Scope: one keypair per environment (`prod`, `staging`).

---

## 5. SKCHAT_TURN_SECRET

- This is the `use-auth-secret` static secret from the shared skstack coturn
  (`/etc/coturn/turnserver.conf`, `static-auth-secret=...`).
- The skchat process uses it only to derive short-lived HMAC credentials
  (`<expiry>:<fqid>` username + HMAC-SHA1 credential); the static secret is
  never sent to the browser or logged.
- Rotate: update coturn + OpenBao + redeploy skchat simultaneously.

---

## 6. Rotation procedure

1. Generate new secret value (LiveKit admin UI, `openssl rand -hex 32`, etc.)
2. Write to OpenBao: `bao kv put secret/skchat/prod/<key> value=<new>`
3. Re-inject to host `.env` (or wait for agent sidecar renewal)
4. Rolling redeploy: `docker service update --force skchat_webui skchat_daemon`
   (voice service if TURN secret changed)
5. Verify health: `curl https://skchat.skstack01.douno.it/health`

---

## 7. What must never happen

- A real `SKCHAT_LIVEKIT_API_SECRET`, `SKCHAT_TURN_SECRET`, or DB password
  committed to git (in a Dockerfile, stack YAML, `.env` file, or any tracked file)
- Secrets in Docker image ENV layers (inspect `docker image history` — must be clean)
- Plain-text secrets in Swarm service inspect output (use Docker secrets or inject
  via the `.env` mechanism above — Swarm service env is visible to managers)
- Unrotated secrets after a container image leak or host compromise

For the full rotation + incident response runbook see `security/runbooks/secret-rotation.md`.

---

## Media plane (LiveKit + coturn)

*Added Batch B4.  These stacks are deployed independently from skchat but share two
secrets with it (`SKCHAT_LIVEKIT_API_SECRET`, `SKCHAT_TURN_SECRET`) — rotate them
together.*

### Host env file locations

| Stack | File | Permissions |
|---|---|---|
| LiveKit | `/var/data/deploy_livekit/livekit.env` | `chmod 600` |
| coturn | `/var/data/deploy_coturn/coturn.env` | `chmod 600` |

Sanitized template (variable names, no values): `deploy/v2/media-plane.env.example`.

### Secret inventory

| Variable | Stack | Purpose | Tier |
|---|---|---|---|
| `LIVEKIT_KEYS` | livekit | API key + secret pair (`"key: secret"`) consumed by `livekit/livekit-server` | P0 — required |
| `SKCHAT_LIVEKIT_API_KEY` | skchat | Same API key, split out for skchat JWT minting | P0 — required (in skchat.env) |
| `SKCHAT_LIVEKIT_API_SECRET` | skchat | Same API secret, split out for skchat JWT minting | P0 — required (in skchat.env) |
| `SKCHAT_TURN_SECRET` | coturn + skchat | `use-auth-secret` static secret; shared between coturn and skchat for HMAC-SHA1 REST credential derivation | P0 — required in BOTH env files |

### What is NOT a secret (safe in stack YAML or public config)

- `SKCHAT_TURN_REALM` — the TURN server hostname/domain (no credentials)
- `COTURN_MIN_PORT` / `COTURN_MAX_PORT` — relay port range bounds
- `LIVEKIT_REDIS_ADDRESS` — Redis address for multi-node (no credentials; add password if Redis is auth-enabled)
- LiveKit `livekit.yaml` config (non-secret tuning: bind port, RTC range, log level, region, TURN disabled)

### OpenBao path mapping

```
secret/data/media/<env>/<key>
```

| Secret | OpenBao path |
|---|---|
| `LIVEKIT_KEYS` | `secret/data/media/prod/livekit_keys` |
| `SKCHAT_TURN_SECRET` | `secret/data/media/prod/turn_secret` |

*`SKCHAT_LIVEKIT_API_KEY` and `SKCHAT_LIVEKIT_API_SECRET` are the same values as
`LIVEKIT_KEYS` split for the skchat consumer — store once under the media path,
reference from `secret/data/skchat/prod/livekit_api_key` (or deduplicate via an
OpenBao alias).*

### Shared-secret rule

`SKCHAT_TURN_SECRET` **must be identical** in `coturn.env` and `skchat.env`.
Rotation steps:

1. `openssl rand -hex 32` → new value
2. Write to OpenBao: `bao kv put secret/media/prod/turn_secret value=<new>`
3. Update both host `.env` files (`coturn.env` and `skchat.env`) — or wait for the
   OpenBao agent sidecar to refresh them
4. Redeploy both stacks simultaneously:
   `docker stack deploy --env-file /var/data/deploy_coturn/coturn.env -c deploy/v2/coturn-stack.yml coturn`
   `docker service update --force skchat_webui skchat_daemon`
5. Verify: TURN credential test from an off-tailnet client

### LiveKit API key rotation

1. Generate a new keypair (`livekit-cli generate-keys` or `openssl rand -hex 32`)
2. Update `LIVEKIT_KEYS` in `livekit.env` and `SKCHAT_LIVEKIT_API_KEY`/`_SECRET` in `skchat.env`
3. Write to OpenBao: `bao kv put secret/media/prod/livekit_keys value="<key>: <secret>"`
4. Redeploy LiveKit stack + rolling skchat update:
   `docker stack deploy --env-file /var/data/deploy_livekit/livekit.env -c deploy/v2/livekit-stack.yml livekit`
   `docker service update --force skchat_webui`
5. Verify: mint a test token from the webui `/livekit/token` endpoint

### Assumptions & version pins

- **LiveKit:** `livekit/livekit-server:v1.7` (stable at time of writing; pin to a digest in prod).
  Single-node (Redis disabled, `replicas: 0` on `livekit-redis`).  Scale to multi-node by setting
  `LIVEKIT_REDIS_ADDRESS` + `livekit-redis` replicas to 1.
- **coturn:** `coturn/coturn:4.6`.  `use-auth-secret` mode only (no per-user accounts).
  TLS (`:5349`) disabled by default; enable by adding cert mount + flags to `coturn-stack.yml`.
- **Host networking:** both stacks use `network_mode: host` so that Tailscale serve can proxy
  LiveKit's WS port and coturn can bind the full relay UDP range.  A Swarm worker node label
  (`livekit=true`, `coturn=true`) pins each service to the correct node.
- **Tailscale serve** must be configured on the host before deploying LiveKit (see the note block
  at the top of `livekit-stack.yml`).  coturn is reachable by its host public IP / domain only
  (off-tailnet guests); tailnet peers never hit coturn.
