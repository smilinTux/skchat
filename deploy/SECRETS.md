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
