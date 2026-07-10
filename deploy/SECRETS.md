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

## Backup & Garage (B5)

*Added Batch B5.  These secrets are used by the backup script and by the Garage
object store itself — not by the skchat Swarm services at runtime.*

### Host env file location

| Component | File | Permissions |
|---|---|---|
| skchat backup | `/var/data/deploy_skchat/backup.env` | `chmod 600` |
| Garage | `/var/data/deploy_garage/garage.env` | `chmod 600` |

The skchat backup env reuses `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` for the
Garage key (same variable names as AWS CLI; rclone + restic read them the same way).
Garage's own env (`garage.env`) is separate and contains the cluster RPC secret.

### Secret inventory

| Variable | Component | Purpose | Tier |
|---|---|---|---|
| `GARAGE_RPC_SECRET` | Garage cluster | Shared secret for inter-node RPC auth; must be identical on all nodes | P0 — required |
| `GARAGE_ADMIN_TOKEN` | Garage admin API | Bearer token for the admin API (port 3903) | P0 — required |
| `AWS_ACCESS_KEY_ID` | skchat backup | Garage S3 key ID for the skchat-backup bucket | P0 — required |
| `AWS_SECRET_ACCESS_KEY` | skchat backup | Garage S3 key secret for the skchat-backup bucket | P0 — required |
| `RESTIC_PASSWORD` | skchat backup | restic repository encryption password | P0 — required |
| `SKCHAT_BACKUP_AGE_PUBKEY` | skchat backup | age public key (recipient) for identity/skcapstone archive encryption | P1 — not secret (public), but must be correct |
| age private key | restore-time only | age private key to decrypt skchat-identity + skchat-skcapstone archives | P0 — offline/OpenBao; NEVER in Garage |

### What is NOT a secret (safe in garage.toml or stack YAML)

- `SKCHAT_GARAGE_ENDPOINT` — the tailnet address of the Garage S3 API (no credentials)
- `SKCHAT_GARAGE_BUCKET` — bucket name (`skchat-backup`)
- `RESTIC_REPOSITORY` — the restic repository URL (the `RESTIC_PASSWORD` is what secures it)
- Garage storage paths (`metadata_dir`, `data_dir`) in `garage.toml`
- Garage S3 region, bind address, log level

### OpenBao path mapping

```
secret/data/skchat/prod/<key>          (skchat backup secrets)
secret/data/garage/<env>/<key>         (Garage cluster secrets)
```

| Secret | OpenBao path |
|---|---|
| `GARAGE_RPC_SECRET` | `secret/data/garage/prod/rpc_secret` |
| `GARAGE_ADMIN_TOKEN` | `secret/data/garage/prod/admin_token` |
| `AWS_ACCESS_KEY_ID` | `secret/data/skchat/prod/garage_s3_key_id` |
| `AWS_SECRET_ACCESS_KEY` | `secret/data/skchat/prod/garage_s3_key_secret` |
| `RESTIC_PASSWORD` | `secret/data/skchat/prod/restic_password` |
| age private key | `secret/data/skchat/prod/backup_age_privkey` (+ offline copy) |

### Critical: age private key custody

The age private key used to decrypt `skchat-identity` and `skchat-skcapstone`
archives **must never be stored in Garage** — that makes the encryption circular.
Required storage:
1. OpenBao on a **separate host** from the Garage cluster.
2. Offline copy (printed / air-gapped USB) held by the operator (Chef).

If this key is lost, the encrypted archives become permanently unrecoverable —
the capauth identity keypair cannot be restored.

### Rotation procedure

**GARAGE_RPC_SECRET:**
1. Generate: `openssl rand -hex 32` → new value
2. Write to OpenBao: `bao kv put secret/garage/prod/rpc_secret value=<new>`
3. Update `/var/data/deploy_garage/garage.env` on ALL Garage nodes simultaneously
4. Rolling restart: `docker service update --force skchat-garage_garage`
5. Verify: `docker exec ... garage status` — all nodes should reconnect

**Garage S3 key (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY):**
1. `garage key create skchat-backup-key-v2`
2. `garage bucket allow skchat-backup --read --write --key skchat-backup-key-v2`
3. Update OpenBao + `/var/data/deploy_skchat/backup.env`
4. Verify a test backup: `/usr/local/bin/skchat-backup`
5. `garage key delete skchat-backup-key` (old key)

**age recipient key:**
1. `age-keygen -o /tmp/new-backup-recipient.txt`
2. Update `SKCHAT_BACKUP_AGE_PUBKEY` in `backup.env` + OpenBao
3. Store the new private key offline + in OpenBao
4. The next backup will use the new key; old archives are still decryptable with the old key
5. Keep the old private key for the retention window (3 months)

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

---

# Part II: systemd path (the live .158 deployment)

Everything above describes the Docker/Swarm stack. The **live** skchat runs as
per-user systemd units on .158, whose secrets are hand-edited EnvironmentFiles
under `~/.config` plus a couple of inline drop-in values. This part documents the
reproducible provisioning story for that path: names-only templates in the repo,
one script that fills them from skvault with 0600 perms, and a dedicated scoped
Postgres role that replaces the inline superuser DSN.

## 8. Provisioning flow

```bash
# 1. unlock the sacred vault (gpg-agent SEAL), required once per session
skvault unlock

# 2. sanity: confirm the vault is open and every referenced entry resolves
deploy/provision-secrets.sh unlock-check
deploy/provision-secrets.sh check          # resolves every token, writes nothing

# 3. materialise all EnvironmentFiles (0600) into ~/.config/... and friends
deploy/provision-secrets.sh apply
#    or a single target:
deploy/provision-secrets.sh apply bridge-memory

# safe preview (no vault access, no writes):
deploy/provision-secrets.sh dry-run
```

- Templates: `deploy/env-templates/*.example` (names only, `${skvault:<title>}`
  placeholders). The render engine is `deploy/render-secrets.py`.
- The script never prints a secret value. It reports only the target path, the
  file mode, and how many secrets were resolved.
- It fails closed: a locked vault, a missing/ambiguous entry, or an unresolved
  token aborts the whole file and writes nothing. Writes are atomic (temp file in
  the target dir, `chmod 0600`, `os.replace`).
- Sandbox testing: set `SKCHAT_PROVISION_DESTDIR=/tmp/somewhere` to prefix every
  target path so a real run never touches `~/.config`.

## 9. Live secret inventory (systemd path)

Names only. `<agent>` variants share a title scheme. Every target is `chmod 0600`.

| Env var | skvault entry title | Target file | Consumed by |
|---|---|---|---|
| `TELEGRAM_OPUS_BOT_TOKEN` | `skchat Telegram Opus Bot Token` | `~/.config/skchat/telegram-opus.env` | `skchat-telegram-opus.service` |
| `SKC_BRIDGE_TOKEN` | `skchat Telegram Lumina Bot Token` | `~/.config/skchat/telegram-lumina.env` | `skchat-telegram-lumina.service` |
| `SKMEMORY_PG_DSN` | `skchat Bridge Postgres Role` (password) | `~/.config/skchat/bridge-memory.env` | both telegram bridge drop-ins |
| `SKCHAT_GUEST_TOKEN_SECRET` | `skchat Guest Token Secret` | `~/.config/skchat/guest-token.env` | `skchat-daemon.d/guest.conf`, `skchat-webui@lumina.d/guest.conf` |
| `SKCHAT_LIVEKIT_API_SECRET` (lumina) | `skchat LiveKit API Secret (lumina)` | `~/.config/skchat/webui-lumina.env` | `skchat-webui@lumina.service` |
| `SKCHAT_LIVEKIT_API_SECRET` (opus) | `skchat LiveKit API Secret (opus)` | `~/.config/skchat/webui-opus.env` | Opus webui |
| `SKCHAT_LIVEKIT_API_SECRET` (chef) | `skchat LiveKit API Secret (chef)` | `~/.config/skchat/webui-chef.env` | Chef webui |
| `SKCHAT_TURN_SECRET` | `skchat coturn TURN Secret` | all `webui-*.env` + `~/.skchat/coturn/coturn.secret` | webui + coturn container |
| `SKCHAT_GUEST_TOKEN_SECRET` | `skchat Guest Token Secret` | `~/.config/skchat/webui-lumina.env` | `skchat-webui@lumina.service` |
| livekit `keys:` (opus/lumina/chef) | `skchat LiveKit API Secret (<agent>)` | `~/.config/livekit/livekit.yaml` | `livekit-server.service` |

Non-secret (safe in git / in the templates as literals): every `SKCHAT_*_URL`,
port, `SKCHAT_LIVEKIT_API_KEY` (the key **id**, e.g. `skchat-lumina`, which must
match a `keys:` name in `livekit.yaml`), `SKMEMORY_VECTOR_BACKEND`,
`SKMEMORY_EMBED_URL/MODEL`, `SKCHAT_TURN_URLS/REALM`. Lines marked `CHANGE_ME` in
the templates are host-specific non-secrets the installer sets per box (tailnet
IP, public funnel URL, operator ids).

> `NVIDIA_API_KEY` in `~/.config/lumina-creative/env` is owned by the
> lumina-creative stack, not skchat; it is out of scope for this script. Rotate it
> via that stack's own tooling.

## 10. Scoped Postgres role for the bridge memory path (rotates the inline DSN)

**Problem.** The live drop-ins
`skchat-telegram-{opus,lumina}.service.d/override.conf` inline
`SKMEMORY_PG_DSN=postgresql://postgres:skmemory@localhost:5432/skmemory`, the
**shared postgres superuser**, also used by skmemory and skingest. That is both a
secret-in-a-drop-in and a massively over-privileged credential for what the
bridge does.

**Fix (do NOT rotate the shared superuser).** Create a dedicated least-privilege
LOGIN role `skchat_bridge` and point the bridges at it. The bridge memory path
(`skmemory/backends/pgvector_backend.py`) only needs:

- `memories`: `SELECT, INSERT, UPDATE, DELETE`
- `docs`: `SELECT` (read-only RAG grounding; docs are written by skingest)
- `CONNECT` on `skmemory`, `USAGE` on schema `public`

No sequence grants (memories.id is app-supplied), no superuser, nothing else. The
exact SQL is `deploy/sql/skchat_bridge_role.sql` (rollback:
`deploy/sql/skchat_bridge_role_rollback.sql`). It was validated against the live
`skmem-pg` container in a `BEGIN; ... ROLLBACK;` transaction (scoped grants
confirmed: `docs=SELECT`, `memories=DELETE,INSERT,SELECT,UPDATE`).

**Cutover (a separate task, NOT performed by the provisioning task):**

```bash
# 1. pick a strong password, store it in skvault as "skchat Bridge Postgres Role"
#    (username skchat_bridge). Then create the role in skmem-pg:
docker exec -i skmem-pg psql -U postgres -d skmemory \
  -v bridge_pw="<STRONG_PW>" -f deploy/sql/skchat_bridge_role.sql

# 2. materialise the new DSN into bridge-memory.env (0600):
skvault unlock && deploy/provision-secrets.sh apply bridge-memory

# 3. reconcile the drop-ins: remove the inline SKMEMORY_PG_DSN= line and add
#    EnvironmentFile=%h/.config/skchat/bridge-memory.env
#    (see the reconcile-units task); then:
systemctl --user daemon-reload
systemctl --user restart skchat-telegram-opus.service skchat-telegram-lumina.service

# 4. verify memory recall still works, then the old inline superuser DSN is gone.
```

Rollback: revert the drop-ins to the shared DSN, restart the bridges, then
`deploy/sql/skchat_bridge_role_rollback.sql`.

## 11. Rotation procedures (systemd path)

| Secret | How to rotate |
|---|---|
| Telegram bot tokens | Regenerate via `@BotFather`, update the skvault entry, `provision-secrets.sh apply telegram-<agent>`, restart the bridge unit. |
| `skchat_bridge` DB password | New password into skvault, re-run `skchat_bridge_role.sql` with `-v bridge_pw=<new>` (it `ALTER ROLE ... PASSWORD`s in place), `provision-secrets.sh apply bridge-memory`, restart both bridges. |
| `SKCHAT_TURN_SECRET` | `openssl rand -hex 32`, update skvault, `provision-secrets.sh apply webui-lumina webui-opus webui-chef coturn`, restart coturn container + webui units (the secret feeds both). |
| `SKCHAT_GUEST_TOKEN_SECRET` | New value into skvault, `provision-secrets.sh apply guest-token webui-lumina`, restart daemon + webui (both consume the same value; they must stay identical). |
| LiveKit key secrets | New secret per key, update the three skvault entries, `provision-secrets.sh apply livekit webui-lumina webui-opus webui-chef`, restart `livekit-server` + webui units. |

Never print a rotated value to a shell that logs history; let the script pull it
from skvault. Never commit a filled-in `.env`, `livekit.yaml`, or `coturn.secret`.

