# skchat v2 — Backup & Restore Runbook (B5)

**Object store:** Garage — sovereign S3-compatible, 3-replica, self-hosted.
Deploy skeleton: `deploy/v2/garage-stack.yml`.
Env template: `deploy/v2/skchat-prod.env.example` (Garage / backup section).

---

## 1. What to back up

| Source | Content | Secret-sensitive? | Backup priority |
|--------|---------|------------------|-----------------|
| Docker volume `skchat_skchat-identity` | capauth Ed25519 private keys, pairing nonces | **YES — encrypt before upload** | P0 — daily |
| Docker volume `skchat_skchat-skcapstone` | skcapstone peer registry, trust FEBs, ritual blobs | **YES — encrypt before upload** | P0 — daily |
| Docker volume `skchat_skchat-data` | history.db, outbox.db, group configs, memory/index.db | No | P1 — daily |
| Docker volume `skchat_skchat-skcomms` | SKComms transport state, pending outbox | No | P2 — daily |
| Docker volume `skchat_skchat-recordings` | Voice-session audio blobs | No (private call audio) | P1 — daily, 30-day retention |
| skmem-pg dump | PostgreSQL memories + docs tables (agent memory) | No (data-sensitive) | P0 — daily |

> **Naming note:** Docker Swarm prefixes named volumes with the stack name.
> When the stack is deployed as `skchat`, the volume name on the host is
> `skchat_skchat-identity`, `skchat_skchat-data`, etc.
> Verify with `docker volume ls | grep skchat`.

### What is NOT backed up here

- LiveKit recordings (backed up from the `livekit-recordings` volume by the
  `livekit` stack operator — see `deploy/v2/livekit-stack.yml`).
- Container images (rebuild from source; not backed up to Garage).
- The host `.env` files — back up separately to OpenBao or an encrypted vault
  on a separate host; do **not** put them in the Garage bucket.

---

## 2. Encryption contract

**Rule: capauth keys (`skchat-identity`) and skcapstone trust state
(`skchat-skcapstone`) MUST be encrypted before upload to Garage.  Never
upload private key material in plaintext to any object store.**

### Recommended encryption: age

```bash
# Generate a recipient key once; store the private key in OpenBao or offline.
age-keygen -o /var/data/backup/backup-recipient.txt
# The public key line (starts with "age1...") goes in SKCHAT_BACKUP_AGE_PUBKEY.
```

The backup script (§3) encrypts the identity + skcapstone tarballs with this
recipient key before handing them to restic.  The private key is only needed
at restore time.

Recordings and skchat-data are backed up unencrypted (restic encrypts the
restic repository itself with `RESTIC_PASSWORD`, but the Garage bucket should
also have SSE-S3 enabled — see §5 "Garage setup").

---

## 3. Backup script

Path on the deployment host: `/usr/local/bin/skchat-backup`
Run as: the user that owns the Docker volumes (typically `root`).

```bash
#!/usr/bin/env bash
# skchat-backup — daily restic backup of skchat Swarm volumes to Garage S3.
# Reads from: /var/data/deploy_skchat/backup.env  (chmod 600)
# Logs to:    /var/log/skchat-backup.log
set -euo pipefail
LOG=/var/log/skchat-backup.log
exec >> "$LOG" 2>&1

source /var/data/deploy_skchat/backup.env   # RESTIC_REPOSITORY, RESTIC_PASSWORD,
                                            # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
                                            # SKCHAT_BACKUP_AGE_PUBKEY

STACK=skchat
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "=== skchat backup $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# ── 1. SECRET volumes — encrypt with age before restic ───────────────────────
for VOLNAME in skchat-identity skchat-skcapstone; do
    VOL="${STACK}_${VOLNAME}"
    TAR="${TMPDIR}/${VOLNAME}.tar"
    ENC="${TMPDIR}/${VOLNAME}.tar.age"
    echo "  Archiving volume: ${VOL}"
    docker run --rm \
        -v "${VOL}:/src:ro" \
        -v "${TMPDIR}:/dst" \
        busybox:1.36 \
        tar -C /src -cf "/dst/${VOLNAME}.tar" .
    age -r "$SKCHAT_BACKUP_AGE_PUBKEY" -o "$ENC" "$TAR"
    rm -f "$TAR"
    echo "  Encrypted: ${ENC}"
done

# ── 2. NON-SECRET volumes — restic snapshots (restic repo is also encrypted) ──
for VOLNAME in skchat-data skchat-skcomms; do
    VOL="${STACK}_${VOLNAME}"
    echo "  Snapshotting volume: ${VOL}"
    docker run --rm \
        -v "${VOL}:/data/volume:ro" \
        -e RESTIC_REPOSITORY="$RESTIC_REPOSITORY" \
        -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
        -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
        -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
        restic/restic:0.17 backup /data/volume \
            --tag "${VOLNAME}" \
            --tag "skchat" \
            --host "$(hostname)"
done

# ── 3. Recordings volume — 30-day retention ───────────────────────────────────
VOL="${STACK}_skchat-recordings"
echo "  Snapshotting recordings volume: ${VOL}"
docker run --rm \
    -v "${VOL}:/data/volume:ro" \
    -e RESTIC_REPOSITORY="$RESTIC_REPOSITORY/recordings" \
    -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
    -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    restic/restic:0.17 backup /data/volume \
        --tag "skchat-recordings" \
        --host "$(hostname)"

# ── 4. Upload age-encrypted secret tarballs to Garage via rclone ─────────────
for VOLNAME in skchat-identity skchat-skcapstone; do
    ENC="${TMPDIR}/${VOLNAME}.tar.age"
    DEST="garage:${SKCHAT_GARAGE_BUCKET}/secrets/$(date -u +%Y-%m-%d)/${VOLNAME}.tar.age"
    echo "  Uploading: ${ENC} → ${DEST}"
    rclone copyto "$ENC" "$DEST" \
        --s3-provider=Other \
        --s3-endpoint="$SKCHAT_GARAGE_ENDPOINT" \
        --s3-access-key-id="$AWS_ACCESS_KEY_ID" \
        --s3-secret-access-key="$AWS_SECRET_ACCESS_KEY"
done

# ── 5. skmem-pg dump ──────────────────────────────────────────────────────────
# pg_dump into restic via stdin (no plaintext dump file on disk).
echo "  Dumping skmem-pg..."
PGPASSWORD="$SKMEMORY_PG_PASSWORD" \
    pg_dump \
        -h "$SKMEMORY_PG_HOST" \
        -p "${SKMEMORY_PG_PORT:-5432}" \
        -U "$SKMEMORY_PG_USER" \
        --dbname "$SKMEMORY_PG_DB" \
        --format=custom \
        --no-password \
    | docker run --rm -i \
        -e RESTIC_REPOSITORY="$RESTIC_REPOSITORY/pg" \
        -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
        -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
        -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
        restic/restic:0.17 backup \
            --stdin \
            --stdin-filename "skmemory.pgdump" \
            --tag "skmem-pg" \
            --tag "skchat" \
            --host "$(hostname)"

# ── 6. Prune old snapshots ────────────────────────────────────────────────────
echo "  Pruning snapshots (keep 7 daily, 4 weekly, 3 monthly)..."
for REPO_SUFFIX in "" "/recordings" "/pg"; do
    docker run --rm \
        -e RESTIC_REPOSITORY="${RESTIC_REPOSITORY}${REPO_SUFFIX}" \
        -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
        -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
        -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
        restic/restic:0.17 forget \
            --prune \
            --keep-daily 7 \
            --keep-weekly 4 \
            --keep-monthly 3
done

# Recordings: shorter — 30 days, no weekly/monthly carry-forward.
docker run --rm \
    -e RESTIC_REPOSITORY="${RESTIC_REPOSITORY}/recordings" \
    -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
    -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    restic/restic:0.17 forget \
        --prune \
        --keep-within 30d

echo "=== backup complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
```

### Installation

```bash
sudo cp scripts/skchat-backup /usr/local/bin/skchat-backup
sudo chmod 700 /usr/local/bin/skchat-backup

# Host backup env (separate from the deploy env — add the backup-specific vars)
sudo cp deploy/v2/skchat-prod.env.example /var/data/deploy_skchat/backup.env
sudo chmod 600 /var/data/deploy_skchat/backup.env
sudo $EDITOR /var/data/deploy_skchat/backup.env   # fill in Garage + restic vars

# Initialize the restic repositories once:
for SUFFIX in "" "/recordings" "/pg"; do
    source /var/data/deploy_skchat/backup.env
    docker run --rm \
        -e RESTIC_REPOSITORY="${RESTIC_REPOSITORY}${SUFFIX}" \
        -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
        -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
        -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
        restic/restic:0.17 init
done
```

---

## 4. Schedule (cron)

```cron
# /etc/cron.d/skchat-backup  (or systemd timer — see below)
# Run daily at 03:00 UTC, log to /var/log/skchat-backup.log
0 3 * * *  root  /usr/local/bin/skchat-backup
```

**Recommended: systemd timer** (more observable, restart-on-failure):

```ini
# /etc/systemd/system/skchat-backup.service
[Unit]
Description=skchat Garage backup
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/skchat-backup
StandardOutput=journal
StandardError=journal
User=root

# /etc/systemd/system/skchat-backup.timer
[Unit]
Description=skchat daily backup

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now skchat-backup.timer
sudo systemctl list-timers skchat-backup.timer
```

---

## 5. Garage setup (bucket + SSE)

See `deploy/v2/garage-stack.yml` for the Garage Swarm stack.  Once Garage is
running, bootstrap the backup bucket:

```bash
# Create the backup bucket
garage bucket create skchat-backup

# Allow the backup key to read/write the bucket
garage key create skchat-backup-key
garage bucket allow skchat-backup --read --write --key skchat-backup-key

# Display the key credentials → add to backup.env
garage key info skchat-backup-key

# Enable SSE-S3 server-side encryption on the bucket (Garage ≥ 1.0 required)
# This encrypts all objects at rest on Garage nodes (AES-256).
# NOTE: age encryption on the identity/skcapstone archives is an *additional*
# layer on top of SSE-S3 — both are required for those secret volumes.
garage bucket website skchat-backup   # (adjust per Garage version CLI)
```

> **Garage endpoint URL** (tailnet, default Garage S3 API port):
> `http://garage.skchat-garage:3900`  (inside the skchat-garage Swarm stack overlay)
> or the tailnet IP/hostname of the node running Garage if accessed from the host.

---

## 6. Retention policy

| Data | Daily | Weekly | Monthly | Max age |
|------|-------|--------|---------|---------|
| skchat-data | 7 | 4 | 3 | ~3 months |
| skchat-skcomms | 7 | 4 | 3 | ~3 months |
| skchat-identity (age-encrypted) | kept in Garage with date prefix | — | — | 3 months (delete old dates manually) |
| skchat-skcapstone (age-encrypted) | kept in Garage with date prefix | — | — | 3 months |
| skchat-recordings | 30 days | — | — | 30 days |
| skmem-pg | 7 | 4 | 3 | ~3 months |

---

## 7. Restore procedure

### 7.1 Restore a non-secret volume (example: skchat-data)

```bash
source /var/data/deploy_skchat/backup.env

# 1. List snapshots to find the target
docker run --rm \
    -e RESTIC_REPOSITORY="$RESTIC_REPOSITORY" \
    -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
    -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    restic/restic:0.17 snapshots --tag skchat-data

# 2. Stop the skchat stack (prevents writes during restore)
docker stack rm skchat

# 3. Restore to a temporary volume, then swap
docker volume create skchat_skchat-data-restore
docker run --rm \
    -v skchat_skchat-data-restore:/restore \
    -e RESTIC_REPOSITORY="$RESTIC_REPOSITORY" \
    -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
    -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    restic/restic:0.17 restore latest --tag skchat-data \
        --target /restore

# 4. Rename volumes (swap)
docker volume rm skchat_skchat-data
docker volume create skchat_skchat-data
docker run --rm \
    -v skchat_skchat-data-restore:/src:ro \
    -v skchat_skchat-data:/dst \
    busybox:1.36 sh -c 'cp -a /src/. /dst/'
docker volume rm skchat_skchat-data-restore

# 5. Redeploy
set -a && source /var/data/deploy_skchat/skchat.env && set +a
docker stack deploy --env-file /var/data/deploy_skchat/skchat.env \
    -c deploy/v2/skchat-stack.yml skchat
```

### 7.2 Restore secret volume (skchat-identity or skchat-skcapstone)

```bash
# Requires: age private key (stored offline or in OpenBao — NOT in Garage)
AGE_PRIVKEY=/path/to/backup-recipient-private.txt   # from secure storage
VOLNAME=skchat-identity   # or skchat-skcapstone
BACKUP_DATE=2026-06-13    # target date prefix in Garage

source /var/data/deploy_skchat/backup.env

# 1. Download the age-encrypted archive
rclone copyto \
    "garage:${SKCHAT_GARAGE_BUCKET}/secrets/${BACKUP_DATE}/${VOLNAME}.tar.age" \
    "/tmp/${VOLNAME}.tar.age" \
    --s3-provider=Other \
    --s3-endpoint="$SKCHAT_GARAGE_ENDPOINT" \
    --s3-access-key-id="$AWS_ACCESS_KEY_ID" \
    --s3-secret-access-key="$AWS_SECRET_ACCESS_KEY"

# 2. Decrypt
age -d -i "$AGE_PRIVKEY" -o "/tmp/${VOLNAME}.tar" "/tmp/${VOLNAME}.tar.age"

# 3. Stop the stack, restore, restart (same swap pattern as §7.1)
docker stack rm skchat
docker volume rm "skchat_${VOLNAME}" || true
docker volume create "skchat_${VOLNAME}"
docker run --rm \
    -v "/tmp/${VOLNAME}.tar:/src.tar:ro" \
    -v "skchat_${VOLNAME}:/dst" \
    busybox:1.36 tar -C /dst -xf /src.tar
rm -f "/tmp/${VOLNAME}.tar" "/tmp/${VOLNAME}.tar.age"
set -a && source /var/data/deploy_skchat/skchat.env && set +a
docker stack deploy --env-file /var/data/deploy_skchat/skchat.env \
    -c deploy/v2/skchat-stack.yml skchat

# 4. Verify identity resolved correctly
docker exec $(docker ps -qf name=skchat_daemon) skchat identity
```

### 7.3 Restore skmem-pg

```bash
source /var/data/deploy_skchat/backup.env

# 1. List snapshots
docker run --rm \
    -e RESTIC_REPOSITORY="${RESTIC_REPOSITORY}/pg" \
    -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
    -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    restic/restic:0.17 snapshots --tag skmem-pg

# 2. Restore dump to a file, then restore into Postgres
docker run --rm \
    -v /tmp:/restore \
    -e RESTIC_REPOSITORY="${RESTIC_REPOSITORY}/pg" \
    -e RESTIC_PASSWORD="$RESTIC_PASSWORD" \
    -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    restic/restic:0.17 restore latest --tag skmem-pg \
        --target /restore

PGPASSWORD="$SKMEMORY_PG_PASSWORD" \
    pg_restore \
        -h "$SKMEMORY_PG_HOST" \
        -p "${SKMEMORY_PG_PORT:-5432}" \
        -U "$SKMEMORY_PG_USER" \
        --dbname "$SKMEMORY_PG_DB" \
        --clean \
        --if-exists \
        /tmp/restore/skmemory.pgdump
```

---

## 8. Secret key custody

| Key | Purpose | Storage |
|-----|---------|---------|
| `RESTIC_PASSWORD` | Encrypts the restic repository | OpenBao `secret/data/skchat/prod/restic_password` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Garage S3 backup key | OpenBao `secret/data/skchat/prod/garage_backup_key` |
| age recipient private key | Decrypt identity/skcapstone archives | Offline (printed + secured) AND OpenBao |
| age recipient public key (`SKCHAT_BACKUP_AGE_PUBKEY`) | Encrypt identity/skcapstone archives | Non-secret; in `backup.env` and this file |

**The age private key must never be stored in Garage** — that would make the
encryption circular (the key that unlocks the backup is itself inside the backup).
Store it in OpenBao on a separate host, and/or offline (air-gapped / paper copy).

---

## 9. Assumptions & cluster notes

- **restic ≥ 0.17**, **rclone ≥ 1.67**, **age ≥ 1.1** installed on the backup host
  (or used via the `restic/restic:0.17` Docker image as shown above).
- **pg_dump / pg_restore** installed on the backup host (or run inside a
  `postgres:17-alpine` container with `network_mode: host`).
- **Garage** is deployed on the same tailnet and reachable from the backup host
  — see `deploy/v2/garage-stack.yml`.  The S3 API endpoint (`SKCHAT_GARAGE_ENDPOINT`)
  should be the tailnet address, not a public URL.
- All Docker volume names assume the stack was deployed as `skchat`; adjust
  if you deploy under a different stack name.
- The backup script runs `docker run --rm -v <vol>:/src:ro busybox tar` to read
  volume contents without stopping the container — this is safe for SQLite
  databases (the WAL-mode SQLite in skchat flushes before a hot-backup).  For
  the highest consistency guarantee, stop the daemon before backing up `skchat-data`.
- The skmem-pg backup uses `pg_dump --format=custom` which is portable and
  supports partial restores.  `pg_restore --clean --if-exists` replaces
  existing tables; ensure downstream services (skingest, skwhisper) are quiesced.
