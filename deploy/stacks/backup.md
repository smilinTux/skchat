# skchat comms-suite — Backup & Restore (B5)

State volumes for the same-origin comms stack (`deploy/stacks/skchat-stack.yml`),
backed up to **Garage** (sovereign S3-compatible object store, tailnet-only).
Garage deploy skeleton: `deploy/v2/garage-stack.yml`.

---

## 1. What to back up

Swarm prefixes named volumes with the stack name. Deployed as `skchat`, the
on-host volume names are `skchat_<volume>`. Verify: `docker volume ls | grep skchat`.

| Volume | Content | Secret-sensitive? | Priority |
|--------|---------|-------------------|----------|
| `skchat_skchat-identity` | capauth Ed25519 **private keys** + pairing nonces | **YES — encrypt before upload** | P0 daily |
| `skchat_skchat-skcapstone` | peer registry, trust FEBs, ritual blobs, peer pubkeys | **YES — encrypt before upload** | P0 daily |
| `skchat_skchat-data` | history.db, outbox.db, group configs, memory/index.db | No | P1 daily |
| `skchat_skchat-outbox` | SKComms transport state, pending outbox queue | No | P2 daily |
| `skchat_skchat-recordings` | voice-session audio (private call audio) | No (private) | P1 daily, 30d retention |
| skmem-pg dump | Postgres `memories` + `docs` tables | No (data-sensitive) | P0 daily |

### MUST NOT be in any image layer (and not in the Garage bucket as plaintext)

- **Identity private keys** (`skchat-identity`) — encrypt with `age` before upload.
- **skcapstone trust state** (`skchat-skcapstone`) — encrypt with `age` before upload.
- **Recordings** (`skchat-recordings`) — private call audio; never bake into an
  image; rely on Garage bucket-level encryption (SSE) for at-rest protection.
- The Docker **secrets** themselves (`livekit_api_key`, `turn_secret`, …) live in
  the Swarm raft, not on these volumes — back them up via OpenBao, NOT here.
- Host `.env` / `.env.secrets` — back up to OpenBao/offline, NOT to the bucket.

---

## 2. Encryption contract

**Rule: the two SECRET-SENSITIVE volumes (`skchat-identity`, `skchat-skcapstone`)
MUST be encrypted client-side before they ever reach Garage. Never upload private
key material in plaintext to any object store.**

```bash
# Generate the recipient key ONCE; store the PRIVATE key in OpenBao / offline.
age-keygen -o /var/data/backup/backup-recipient.txt
# The public line (age1...) goes in SKCHAT_BACKUP_AGE_PUBKEY (backup.env).
# OpenBao path for the private key: secret/data/skchat/prod/backup_age_privkey
```

restic (below) ALSO encrypts the whole repo with `RESTIC_PASSWORD`; the `age`
layer is defence-in-depth specifically for the identity/trust tarballs so that
even a leaked restic password never exposes raw private keys.

---

## 3. Backup script sketch — `/usr/local/bin/skchat-backup`

Targets Garage via restic's native S3 backend. Reads non-secret config + the
Garage key / restic password / age pubkey from `/var/data/deploy_skchat/backup.env`
(chmod 600, gitignored, never committed).

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a; source /var/data/deploy_skchat/backup.env; set +a
# backup.env provides:
#   SKCHAT_GARAGE_ENDPOINT   e.g. http://noroc2027.tail204f0c.ts.net:3900
#   SKCHAT_GARAGE_BUCKET     e.g. skchat-backup
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   (garage key info skchat-backup-key)
#   RESTIC_REPOSITORY        s3:${SKCHAT_GARAGE_ENDPOINT}/${SKCHAT_GARAGE_BUCKET}
#   RESTIC_PASSWORD          (P0 secret — OpenBao secret/data/skchat/prod/restic_password)
#   SKCHAT_BACKUP_AGE_PUBKEY age1...

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
DOCKER_VOL_ROOT=/var/lib/docker/volumes

dump_vol() {  # $1 = swarm volume name → tar.gz in $WORK
  local v="$1"
  tar -C "${DOCKER_VOL_ROOT}/${v}/_data" -czf "${WORK}/${v}.tar.gz" .
}

# --- 1. Plain (non-secret) volumes → tar.gz ---
for v in skchat_skchat-data skchat_skchat-outbox skchat_skchat-recordings; do
  dump_vol "$v"
done

# --- 2. SECRET volumes → tar.gz then age-encrypt (private keys never leave plaintext-on-disk briefly only in $WORK tmpfs) ---
for v in skchat_skchat-identity skchat_skchat-skcapstone; do
  dump_vol "$v"
  age -r "$SKCHAT_BACKUP_AGE_PUBKEY" -o "${WORK}/${v}.tar.gz.age" "${WORK}/${v}.tar.gz"
  shred -u "${WORK}/${v}.tar.gz"   # remove the plaintext tar; keep only .age
done

# --- 3. skmem-pg logical dump (host-reachable Postgres) ---
PGPASSWORD="$SKMEMORY_PG_PASSWORD" pg_dump \
  -h "$SKMEMORY_PG_HOST" -p "${SKMEMORY_PG_PORT:-5432}" \
  -U "$SKMEMORY_PG_USER" -d "${SKMEMORY_PG_DB:-skmemory}" \
  -Fc -f "${WORK}/skmem-pg-${STAMP}.dump"

# --- 4. Ship everything to Garage via restic (repo-level encryption + dedup) ---
restic snapshot 2>/dev/null || restic init     # one-time init (idempotent guard)
restic backup --tag "skchat-${STAMP}" "$WORK"

# --- 5. Retention: keep 7 daily, 4 weekly, 6 monthly; prune ---
restic forget --tag-like "skchat-" \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune

# (recordings handled by the same daily snapshot; 30-day retention enforced by a
#  separate `restic forget --tag recordings --keep-within 30d` if recordings are
#  snapshotted under their own tag — see §6.)
```

Schedule (systemd timer or cron) — daily 03:30 local:

```ini
# /etc/systemd/system/skchat-backup.timer
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
[Install]
WantedBy=timers.target
```

---

## 4. Garage bootstrap (once)

See `deploy/v2/garage-stack.yml` header for the full layout/key steps. Summary:

```bash
G=$(docker ps -qf name=skchat-garage_garage)
docker exec $G garage bucket create skchat-backup
docker exec $G garage key create skchat-backup-key
docker exec $G garage bucket allow skchat-backup --read --write --key skchat-backup-key
docker exec $G garage key info skchat-backup-key      # → AWS_ACCESS_KEY_ID / SECRET
```

Enable bucket-level encryption (SSE) for the recordings at-rest layer.

---

## 5. Restore

```bash
set -a; source /var/data/deploy_skchat/backup.env; set +a

# 1. Pull the latest snapshot to a staging dir
restic restore latest --target /var/data/restore

# 2. Decrypt the secret tarballs (age private key from OpenBao / offline):
age -d -i /var/data/backup/backup-recipient.txt \
  -o /var/data/restore/skchat_skchat-identity.tar.gz \
     /var/data/restore/skchat_skchat-identity.tar.gz.age
age -d -i /var/data/backup/backup-recipient.txt \
  -o /var/data/restore/skchat_skchat-skcapstone.tar.gz \
     /var/data/restore/skchat_skchat-skcapstone.tar.gz.age

# 3. Stop the stack, recreate empty volumes, untar into each, redeploy:
docker stack rm skchat
for v in skchat-data skchat-outbox skchat-identity skchat-skcapstone skchat-recordings; do
  docker volume create "skchat_${v}"
  tar -C "/var/lib/docker/volumes/skchat_${v}/_data" \
      -xzf "/var/data/restore/skchat_${v}.tar.gz"
done

# 4. skmem-pg restore:
PGPASSWORD="$SKMEMORY_PG_PASSWORD" pg_restore -h "$SKMEMORY_PG_HOST" \
  -U "$SKMEMORY_PG_USER" -d "${SKMEMORY_PG_DB}" --clean \
  /var/data/restore/skmem-pg-*.dump

# 5. Re-create secrets + redeploy (supervised):
deploy/stacks/make-secrets.sh
docker stack deploy --env-file /var/data/deploy_skchat/skchat.env \
  -c deploy/stacks/skchat-stack.yml skchat
```

> **Identity restore is the critical path.** If `skchat-identity` is lost AND the
> age-encrypted backup is unrecoverable, the agent's sovereign capauth identity is
> permanently gone — peers will reject its signatures. Test the identity restore
> path on a staging stack at least once per quarter.

---

## 6. Notes

- LiveKit recordings live on the B4 `livekit-recordings` volume and are backed up
  by the media-plane operator (`deploy/v2/livekit-stack.yml`), not here.
- `skchat-recordings` (the voice STT/TTS pipeline) IS covered here; give it its
  own restic tag if you want the 30-day window independent of the P0 volumes.
- Garage is tailnet-only; no `tailscale serve` rule is configured for it.
- Rotate the Garage key + restic password per the OpenBao rotation procedure in
  `deploy/SECRETS.md §6`.
