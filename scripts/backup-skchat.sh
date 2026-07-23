#!/usr/bin/env bash
# backup-skchat.sh - encrypted backup of the skchat stateful data set.
#
# Tars the live-critical, irreplaceable state (at-rest encryption keys,
# message-log/history DB, group-key stores, nonce/conf state, coturn config,
# the skcomms outbox, and the bot-token env files), encrypts the tar to
# Chef's PGP recipient, and writes it to a per-home backup dir. Losing
# atrest_recipient.key means encrypted history is unrecoverable, so this
# script NEVER writes an unencrypted archive to disk: the plaintext tar is
# a mktemp file removed (shredded, when available) by a trap on every exit
# path, and if no usable recipient key is found the script fails loudly
# instead of falling back to writing plaintext.
#
# Usage:
#   backup-skchat.sh [--home DIR] [--stamp STAMP] [--recipient EMAIL]
#                     [--recipient-cert FILE] [--backup-dir DIR]
#                     [--keep-days N]
#
#   --home DIR           Base home dir the source paths are relative to.
#                         Default: $HOME. Override in tests to point at a
#                         scratch home instead of the live one.
#   --stamp STAMP         Timestamp for the output filename. Default:
#                         `date -u +%Y%m%dT%H%M%SZ`. The daemon/timer
#                         environment can forbid `date` in some restricted
#                         eval contexts, hence the override.
#   --recipient EMAIL     gpg recipient to encrypt to. Default: chef@skworld.io.
#   --recipient-cert FILE Optional sq (Sequoia) recipient cert file, used
#                         only as a fallback when gpg has no usable key for
#                         --recipient. Default: <backup-dir>/chef-recipient.cert
#                         if present.
#   --backup-dir DIR      Where encrypted archives land. Default:
#                         <home>/.skchat-backups.
#   --keep-days N         Prune archives older than N days. Default: 14.
#
# Output: <backup-dir>/skchat-<STAMP>.tar.gz.pgp
#
# Encrypt tool selection (runtime detection, in order):
#   1. gpg  - used if `gpg --list-keys <recipient>` finds a public key.
#   2. sq   - used if a --recipient-cert file is given/found and gpg has no key.
#   3. Neither found -> FAIL LOUDLY (exit 1), no archive is written.

set -euo pipefail

HOME_DIR="${HOME}"
STAMP=""
RECIPIENT="chef@skworld.io"
RECIPIENT_CERT=""
BACKUP_DIR=""
KEEP_DAYS=14

while [[ $# -gt 0 ]]; do
    case "$1" in
        --home)           HOME_DIR="$2"; shift 2 ;;
        --stamp)          STAMP="$2"; shift 2 ;;
        --recipient)      RECIPIENT="$2"; shift 2 ;;
        --recipient-cert) RECIPIENT_CERT="$2"; shift 2 ;;
        --backup-dir)     BACKUP_DIR="$2"; shift 2 ;;
        --keep-days)      KEEP_DAYS="$2"; shift 2 ;;
        -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; exit 1 ;;
    esac
done

[[ -n "$STAMP" ]] || STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
[[ -n "$BACKUP_DIR" ]] || BACKUP_DIR="${HOME_DIR}/.skchat-backups"
[[ -n "$RECIPIENT_CERT" ]] || RECIPIENT_CERT="${BACKUP_DIR}/chef-recipient.cert"

SRC_SKCHAT="${HOME_DIR}/.skchat"
SRC_SKCOMMS_OUTBOX="${HOME_DIR}/.skcomms/outbox"
SRC_CONFIG_ENV_DIR="${HOME_DIR}/.config/skchat"

TMP_TAR=""
TMP_RAW=""
STAGING=""
# Shred a single file if it exists (falls back to rm when shred is absent).
_shred_file() {
    local f="$1"
    [[ -n "$f" && -f "$f" ]] || return 0
    if command -v shred >/dev/null 2>&1; then
        shred -u -z "$f" 2>/dev/null || rm -f "$f"
    else
        rm -f "$f"
    fi
}
# Idempotent: safe to call repeatedly (blanks the vars after removal). Shreds
# BOTH plaintext tars (compressed + uncompressed) and every DB snapshot in the
# staging dir, since those snapshots are copies of key-adjacent sqlite DBs and
# must never survive unencrypted on disk.
cleanup() {
    _shred_file "$TMP_TAR"
    _shred_file "$TMP_RAW"
    if [[ -n "$STAGING" && -d "$STAGING" ]]; then
        while IFS= read -r -d '' sf; do
            _shred_file "$sf"
        done < <(find "$STAGING" -type f -print0 2>/dev/null)
        rm -rf "$STAGING"
    fi
    TMP_TAR=""
    TMP_RAW=""
    STAGING=""
}
# Terminating signals must exit, not fall through to statements that
# reference the just-deleted temp file. EXIT runs cleanup once more (no-op
# after a signal handler already ran it, since cleanup is idempotent).
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
trap cleanup EXIT

log()  { echo "[backup-skchat] $*"; }
fail() { echo "[backup-skchat] FATAL: $*" >&2; exit 1; }

# --- 1. Collect source paths that actually exist ---------------------------

TAR_ARGS=()
found_any=0

# Exclude regenerable / bulky non-critical data. The irreplaceable data (the
# at-rest keys, group keys, message history, consumed-nonces, confs) is tiny;
# rotating daemon logs and voice recordings are large and regenerable, so they
# do not belong in a daily encrypted backup. (memory/ is kept: it is not
# obviously regenerable here; exclude it too if it is Syncthing-synced.)
TAR_ARGS+=(
    --exclude=".skchat/*.log"
    --exclude=".skchat/*.log.*"
    --exclude=".skchat/lumina-recordings"
    --exclude=".skchat/cache"
)

# Exclude the LIVE, daemon-written sqlite DBs from the raw tar; a consistent
# snapshot of each is taken separately below (sqlite3 .backup) and appended, so
# the archive never captures a torn mid-write DB. Both patterns are listed
# because a top-level DB (.skchat/message_log.db) and a nested one
# (.skchat/pqc/dm_sessions.db) must both be excluded.
TAR_ARGS+=(
    --exclude=".skchat/*.db"
    --exclude=".skchat/**/*.db"
)

if [[ -d "$SRC_SKCHAT" ]]; then
    TAR_ARGS+=(-C "$HOME_DIR" ".skchat")
    found_any=1
else
    log "WARNING: source missing, skipping: ${SRC_SKCHAT}"
fi

if [[ -d "$SRC_SKCOMMS_OUTBOX" ]]; then
    TAR_ARGS+=(-C "$HOME_DIR" ".skcomms/outbox")
    found_any=1
else
    log "WARNING: source missing, skipping: ${SRC_SKCOMMS_OUTBOX}"
fi

env_files=()
if [[ -d "$SRC_CONFIG_ENV_DIR" ]]; then
    while IFS= read -r -d '' f; do
        env_files+=("$f")
    done < <(find "$SRC_CONFIG_ENV_DIR" -maxdepth 1 -name '*.env' -print0 2>/dev/null)
fi
if [[ ${#env_files[@]} -gt 0 ]]; then
    for f in "${env_files[@]}"; do
        TAR_ARGS+=(-C "$HOME_DIR" ".config/skchat/$(basename "$f")")
    done
    found_any=1
else
    log "WARNING: no *.env files found under ${SRC_CONFIG_ENV_DIR}, skipping"
fi

[[ $found_any -eq 1 ]] || fail "none of the expected source paths exist under ${HOME_DIR}; nothing to back up"

# --- 2. Choose encryption tool (fail loudly if neither is usable) ----------

ENCRYPT_TOOL=""
if command -v gpg >/dev/null 2>&1 && gpg --batch --list-keys "$RECIPIENT" >/dev/null 2>&1; then
    ENCRYPT_TOOL="gpg"
elif command -v sq >/dev/null 2>&1 && [[ -f "$RECIPIENT_CERT" ]]; then
    ENCRYPT_TOOL="sq"
else
    fail "no usable PGP recipient key found for '${RECIPIENT}' (checked gpg keyring and sq recipient cert '${RECIPIENT_CERT}'). Refusing to write an unencrypted backup of at-rest keys. Import a recipient key/cert and retry."
fi
log "encrypt tool: ${ENCRYPT_TOOL} (recipient: ${RECIPIENT})"

# --- 3. Build the plaintext tar in a temp file ------------------------------

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# 3a. Consistent snapshot of every live sqlite DB under ~/.skchat, into a
# staging tree that mirrors the relative path. sqlite3 .backup takes a
# transactionally-consistent copy even while the daemon is writing; a raw tar
# of a live DB can capture a corrupt, unrestorable file. If a *.db turns out
# not to be a sqlite file (or .backup fails), fall back to a plain cp of that
# one file and log it, never abort.
STAGING="$(mktemp -d "${BACKUP_DIR}/.skchat-db-staging-XXXXXX")"
chmod 700 "$STAGING"

# Resolve the sqlite3 CLI robustly: the systemd unit runs with a restricted
# PATH that does NOT include Homebrew, where sqlite3 lives on this host. If it
# is not found at all, every DB falls back to a raw cp (with a loud warning) so
# a backup is still produced, but it will not be a torn-write-safe snapshot.
SQLITE3="$(command -v sqlite3 2>/dev/null || true)"
if [[ -z "$SQLITE3" ]]; then
    for cand in /home/linuxbrew/.linuxbrew/bin/sqlite3 \
                "${HOME_DIR}/.skenv/bin/sqlite3" \
                /usr/local/bin/sqlite3 /usr/bin/sqlite3; do
        if [[ -x "$cand" ]]; then SQLITE3="$cand"; break; fi
    done
fi
[[ -n "$SQLITE3" ]] || log "WARNING: sqlite3 not found on PATH or known locations; DBs will be raw-copied (NOT torn-write-safe)"

db_total=0
db_ok=0
if [[ -d "$SRC_SKCHAT" ]]; then
    while IFS= read -r -d '' db; do
        db_total=$((db_total + 1))
        rel="${db#"${HOME_DIR}"/}"      # e.g. .skchat/message_log.db
        dest="${STAGING}/${rel}"
        mkdir -p "$(dirname "$dest")"
        if [[ -n "$SQLITE3" ]] \
           && "$SQLITE3" "$db" ".backup '${dest}'" 2>/dev/null \
           && [[ -s "$dest" ]]; then
            db_ok=$((db_ok + 1))
        else
            log "WARNING: sqlite .backup unavailable/failed for ${rel}, falling back to cp"
            if cp -p "$db" "$dest" 2>/dev/null; then
                db_ok=$((db_ok + 1))
            else
                log "WARNING: cp fallback also failed for ${rel} (DB not captured)"
            fi
        fi
    done < <(find "$SRC_SKCHAT" -type f -name '*.db' -print0 2>/dev/null)
fi
[[ $db_total -gt 0 ]] && log "DB snapshots: ${db_ok}/${db_total} captured consistently into staging"

# 3b. Build an UNCOMPRESSED tar of the live tree (DBs excluded) + outbox + env.
# On a live box the daemon rewrites files while tar reads them; tar then exits 1
# ("file changed as we read it"), which is expected and the snapshot is still
# fine. Only exit >= 2 is a real fatal error. errexit is disabled just around
# the tar call so exit 1 does not abort the whole backup.
TMP_RAW="$(mktemp "${BACKUP_DIR}/.skchat-backup-raw-XXXXXX.tar")"
chmod 600 "$TMP_RAW"

set +e
tar -cf "$TMP_RAW" "${TAR_ARGS[@]}"
tar_rc=$?
set -e
if [[ $tar_rc -eq 0 ]]; then
    :
elif [[ $tar_rc -eq 1 ]]; then
    log "NOTE: tar exit 1 (files changed during read on a live system); snapshot is still consistent, continuing"
else
    fail "tar failed with exit ${tar_rc} (>= 2 is a fatal error)"
fi

# 3c. Append the consistent DB snapshots. Appended from the staging tree, so
# these land at the same .skchat/... paths as the excluded live DBs would have.
# (Append needs an uncompressed archive, hence the two-step build.)
if [[ $db_ok -gt 0 && -d "${STAGING}/.skchat" ]]; then
    tar -rf "$TMP_RAW" -C "$STAGING" ".skchat"
    log "appended ${db_ok} consistent DB snapshot(s) to the archive"
fi

# 3d. Compress into the final plaintext .tar.gz that gets encrypted.
TMP_TAR="$(mktemp "${BACKUP_DIR}/.skchat-backup-plain-XXXXXX.tar.gz")"
chmod 600 "$TMP_TAR"
gzip -c "$TMP_RAW" > "$TMP_TAR"
# The uncompressed intermediate and the DB staging tree are no longer needed;
# shred them now (cleanup also covers them on any early exit).
_shred_file "$TMP_RAW"
TMP_RAW=""
if [[ -n "$STAGING" && -d "$STAGING" ]]; then
    while IFS= read -r -d '' sf; do
        _shred_file "$sf"
    done < <(find "$STAGING" -type f -print0 2>/dev/null)
    rm -rf "$STAGING"
    STAGING=""
fi
log "built plaintext tar: $(du -h "$TMP_TAR" | cut -f1)"

# --- 4. Encrypt ---------------------------------------------------------

OUT_FILE="${BACKUP_DIR}/skchat-${STAMP}.tar.gz.pgp"

case "$ENCRYPT_TOOL" in
    gpg)
        gpg --batch --yes --trust-model always -e -r "$RECIPIENT" -o "$OUT_FILE" "$TMP_TAR"
        ;;
    sq)
        sq encrypt --recipient-file "$RECIPIENT_CERT" --output "$OUT_FILE" "$TMP_TAR"
        ;;
esac

[[ -s "$OUT_FILE" ]] || fail "encryption produced no output at ${OUT_FILE}"
chmod 600 "$OUT_FILE"
log "wrote encrypted backup: ${OUT_FILE} ($(du -h "$OUT_FILE" | cut -f1))"

# --- 5. Prune old backups ---------------------------------------------------

if [[ "$KEEP_DAYS" -gt 0 ]]; then
    pruned=0
    while IFS= read -r -d '' old; do
        rm -f "$old"
        pruned=$((pruned + 1))
    done < <(find "$BACKUP_DIR" -maxdepth 1 -name 'skchat-*.tar.gz.pgp' -mtime "+${KEEP_DAYS}" -print0 2>/dev/null)
    [[ $pruned -gt 0 ]] && log "pruned ${pruned} backup(s) older than ${KEEP_DAYS} days"
fi

log "done"
