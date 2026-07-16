#!/usr/bin/env bash
# test-backup-roundtrip.sh - round-trip test for backup-skchat.sh / restore-skchat.sh.
#
# Self-contained: seeds a scratch "home" (marker files standing in for
# atrest_recipient.key, the history DB, the skcomms outbox, and a bot-token
# env file, PLUS a real sqlite DB with rows to prove the consistent-snapshot
# path), generates an ephemeral throwaway GPG key in a scratch GNUPGHOME so
# the test never touches the real keyring or Chef's real PGP identity, runs
# backup-skchat.sh against the scratch home, wipes, restores into a second
# scratch dir, and asserts (a) the marker file content survived byte-for-byte
# and (b) the restored sqlite DB opens cleanly and its row count matches.
#
# Run: bash scripts/test-backup-roundtrip.sh
# Expected: prints "ROUNDTRIP OK" and exits 0.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SH="${SCRIPT_DIR}/backup-skchat.sh"
RESTORE_SH="${SCRIPT_DIR}/restore-skchat.sh"

[[ -x "$BACKUP_SH" || -f "$BACKUP_SH" ]] || { echo "FAIL: missing ${BACKUP_SH}" >&2; exit 1; }
[[ -x "$RESTORE_SH" || -f "$RESTORE_SH" ]] || { echo "FAIL: missing ${RESTORE_SH}" >&2; exit 1; }

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/skchat-roundtrip-XXXXXX")"
GNUPGHOME_SCRATCH="${WORK_DIR}/gnupg"
SRC_HOME="${WORK_DIR}/src-home"
BACKUP_DIR="${WORK_DIR}/backups"
RESTORE_DIR="${WORK_DIR}/restore"
TEST_RECIPIENT="skchat-roundtrip-test@skchat.invalid"

# Idempotent: safe to call repeatedly.
cleanup() {
    [[ -n "${WORK_DIR:-}" && -d "$WORK_DIR" ]] && rm -rf "$WORK_DIR"
    WORK_DIR=""
}
# Terminating signals must exit rather than fall through to statements that
# reference the just-deleted scratch dir. EXIT re-runs cleanup (no-op after a
# signal handler already ran it, since cleanup is idempotent).
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "[test] scratch dir: ${WORK_DIR}"

# --- 1. Ephemeral GNUPGHOME + throwaway key (never touches the real keyring) --

mkdir -p "$GNUPGHOME_SCRATCH"
chmod 700 "$GNUPGHOME_SCRATCH"
export GNUPGHOME="$GNUPGHOME_SCRATCH"

echo "[test] generating ephemeral throwaway GPG key for ${TEST_RECIPIENT}..."
GENKEY_ERR="$(mktemp "${WORK_DIR}/genkey-err-XXXXXX")"
gpg --batch --gen-key <<EOF 2>"$GENKEY_ERR"
%no-protection
Key-Type: EDDSA
Key-Curve: ed25519
Subkey-Type: ECDH
Subkey-Curve: cv25519
Name-Real: SKChat Roundtrip Test
Name-Email: ${TEST_RECIPIENT}
Expire-Date: 1d
%commit
EOF
rm -f "$GENKEY_ERR"

gpg --batch --list-keys "$TEST_RECIPIENT" >/dev/null 2>&1 \
    || fail "ephemeral test key not found in scratch keyring after gen-key"

# --- 2. Seed a scratch "home" mirroring the real ~/.skchat layout ----------

MARKER_CONTENT="roundtrip-marker-$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9')"

mkdir -p "${SRC_HOME}/.skchat/coturn" "${SRC_HOME}/.skchat/history" \
         "${SRC_HOME}/.skcomms/outbox" "${SRC_HOME}/.config/skchat"

echo "$MARKER_CONTENT" > "${SRC_HOME}/.skchat/atrest_recipient.key"
echo "confs" > "${SRC_HOME}/.skchat/confs.json"
echo "coturn-secret-stand-in" > "${SRC_HOME}/.skchat/coturn/coturn.secret"
echo "outbox-entry" > "${SRC_HOME}/.skcomms/outbox/entry-1.json"
echo "TELEGRAM_TEST_BOT_TOKEN=stand-in" > "${SRC_HOME}/.config/skchat/telegram-test.env"

# A real sqlite DB with rows, at a nested path, to prove the consistent
# snapshot (.backup) + exclude-live-DB + append path end to end. Resolve
# sqlite3 the same robust way the backup script does (Homebrew is off the
# restricted PATH on this host).
SQLITE3="$(command -v sqlite3 2>/dev/null || true)"
if [[ -z "$SQLITE3" ]]; then
    for cand in /home/linuxbrew/.linuxbrew/bin/sqlite3 "${HOME}/.skenv/bin/sqlite3" \
                /usr/local/bin/sqlite3 /usr/bin/sqlite3; do
        [[ -x "$cand" ]] && { SQLITE3="$cand"; break; }
    done
fi
[[ -n "$SQLITE3" ]] || fail "sqlite3 not found; cannot run the DB round-trip case"

DB_ROWS=137
DB_PATH="${SRC_HOME}/.skchat/pqc/dm_sessions.db"
mkdir -p "$(dirname "$DB_PATH")"
"$SQLITE3" "$DB_PATH" \
    "CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT); \
     WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < ${DB_ROWS}) \
     INSERT INTO messages(body) SELECT 'msg-' || x FROM c;" \
    || fail "could not seed the scratch sqlite DB"

seeded_rows="$("$SQLITE3" "$DB_PATH" 'SELECT COUNT(*) FROM messages;')"
[[ "$seeded_rows" == "$DB_ROWS" ]] || fail "seed sanity: expected ${DB_ROWS} rows, got ${seeded_rows}"

# --- 3. Run the backup against the scratch home -----------------------------

echo "[test] running backup-skchat.sh..."
bash "$BACKUP_SH" \
    --home "$SRC_HOME" \
    --stamp "roundtriptest" \
    --recipient "$TEST_RECIPIENT" \
    --backup-dir "$BACKUP_DIR" \
    --keep-days 14

ARCHIVE="${BACKUP_DIR}/skchat-roundtriptest.tar.gz.pgp"
[[ -f "$ARCHIVE" ]] || fail "expected archive not found: ${ARCHIVE}"
[[ -s "$ARCHIVE" ]] || fail "archive is empty: ${ARCHIVE}"

# No unencrypted intermediate may survive in the backup dir: neither a
# compressed tar, the uncompressed raw tar, nor the DB staging tree.
if find "$BACKUP_DIR" -maxdepth 1 \( -name '*.tar.gz' -o -name '*.tar' \) ! -name '*.pgp' | grep -q .; then
    fail "an unencrypted plaintext tar was left behind in ${BACKUP_DIR}"
fi
if find "$BACKUP_DIR" -maxdepth 1 -type d -name '.skchat-db-staging-*' | grep -q .; then
    fail "a DB staging dir was left behind in ${BACKUP_DIR}"
fi

# --- 4. Wipe the source, restore into a fresh scratch dir -------------------

rm -rf "$SRC_HOME"
echo "[test] source wiped, restoring..."

bash "$RESTORE_SH" --target "$RESTORE_DIR" "$ARCHIVE"

RESTORED_MARKER="${RESTORE_DIR}/.skchat/atrest_recipient.key"
[[ -f "$RESTORED_MARKER" ]] || fail "restored marker file missing: ${RESTORED_MARKER}"

restored_content="$(cat "$RESTORED_MARKER")"
[[ "$restored_content" == "$MARKER_CONTENT" ]] \
    || fail "marker content mismatch: expected '${MARKER_CONTENT}', got '${restored_content}'"

# Spot-check the other flat files came back too.
[[ -f "${RESTORE_DIR}/.skchat/coturn/coturn.secret" ]] || fail "restored coturn secret missing"
[[ -f "${RESTORE_DIR}/.skcomms/outbox/entry-1.json" ]] || fail "restored outbox entry missing"
[[ -f "${RESTORE_DIR}/.config/skchat/telegram-test.env" ]] || fail "restored env file missing"

# The consistent-snapshot path: the restored sqlite DB must exist, pass an
# integrity check (proving it is not torn), and have the same row count.
RESTORED_DB="${RESTORE_DIR}/.skchat/pqc/dm_sessions.db"
[[ -f "$RESTORED_DB" ]] || fail "restored sqlite DB missing: ${RESTORED_DB}"

integrity="$("$SQLITE3" "$RESTORED_DB" 'PRAGMA integrity_check;' 2>/dev/null || true)"
[[ "$integrity" == "ok" ]] || fail "restored sqlite DB failed integrity_check: '${integrity}'"

restored_rows="$("$SQLITE3" "$RESTORED_DB" 'SELECT COUNT(*) FROM messages;' 2>/dev/null || true)"
[[ "$restored_rows" == "$DB_ROWS" ]] \
    || fail "restored DB row count mismatch: expected ${DB_ROWS}, got '${restored_rows}'"
echo "[test] restored sqlite DB opened, integrity ok, ${restored_rows} rows match"

echo "ROUNDTRIP OK"
exit 0
