#!/usr/bin/env bash
# test-backup-roundtrip.sh - round-trip test for backup-skchat.sh / restore-skchat.sh.
#
# Self-contained: seeds a scratch "home" (marker files standing in for
# atrest_recipient.key, the history DB, the skcomms outbox, and a bot-token
# env file), generates an ephemeral throwaway GPG key in a scratch GNUPGHOME
# so the test never touches the real keyring or Chef's real PGP identity,
# runs backup-skchat.sh against the scratch home, wipes, restores into a
# second scratch dir, and asserts the marker file content survived
# byte-for-byte.
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

cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "[test] scratch dir: ${WORK_DIR}"

# --- 1. Ephemeral GNUPGHOME + throwaway key (never touches the real keyring) --

mkdir -p "$GNUPGHOME_SCRATCH"
chmod 700 "$GNUPGHOME_SCRATCH"
export GNUPGHOME="$GNUPGHOME_SCRATCH"

echo "[test] generating ephemeral throwaway GPG key for ${TEST_RECIPIENT}..."
gpg --batch --gen-key <<EOF 2>/tmp/skchat-roundtrip-genkey.$$
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
rm -f /tmp/skchat-roundtrip-genkey.$$

gpg --batch --list-keys "$TEST_RECIPIENT" >/dev/null 2>&1 \
    || fail "ephemeral test key not found in scratch keyring after gen-key"

# --- 2. Seed a scratch "home" mirroring the real ~/.skchat layout ----------

MARKER_CONTENT="roundtrip-marker-$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9')"

mkdir -p "${SRC_HOME}/.skchat/coturn" "${SRC_HOME}/.skchat/history" \
         "${SRC_HOME}/.skcomms/outbox" "${SRC_HOME}/.config/skchat"

echo "$MARKER_CONTENT" > "${SRC_HOME}/.skchat/atrest_recipient.key"
echo "history-db-stand-in" > "${SRC_HOME}/.skchat/history/messages.db"
echo "confs" > "${SRC_HOME}/.skchat/confs.json"
echo "coturn-secret-stand-in" > "${SRC_HOME}/.skchat/coturn/coturn.secret"
echo "outbox-entry" > "${SRC_HOME}/.skcomms/outbox/entry-1.json"
echo "TELEGRAM_TEST_BOT_TOKEN=stand-in" > "${SRC_HOME}/.config/skchat/telegram-test.env"

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

# The plaintext tar must never survive in the backup dir.
if find "$BACKUP_DIR" -maxdepth 1 -name '*.tar.gz' ! -name '*.pgp' | grep -q .; then
    fail "an unencrypted plaintext tar was left behind in ${BACKUP_DIR}"
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

# Spot-check the other files came back too.
[[ -f "${RESTORE_DIR}/.skchat/history/messages.db" ]] || fail "restored history DB missing"
[[ -f "${RESTORE_DIR}/.skchat/coturn/coturn.secret" ]] || fail "restored coturn secret missing"
[[ -f "${RESTORE_DIR}/.skcomms/outbox/entry-1.json" ]] || fail "restored outbox entry missing"
[[ -f "${RESTORE_DIR}/.config/skchat/telegram-test.env" ]] || fail "restored env file missing"

echo "ROUNDTRIP OK"
exit 0
