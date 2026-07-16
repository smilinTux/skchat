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
cleanup() {
    if [[ -n "$TMP_TAR" && -f "$TMP_TAR" ]]; then
        if command -v shred >/dev/null 2>&1; then
            shred -u -z "$TMP_TAR" 2>/dev/null || rm -f "$TMP_TAR"
        else
            rm -f "$TMP_TAR"
        fi
    fi
}
trap cleanup EXIT INT TERM

log()  { echo "[backup-skchat] $*"; }
fail() { echo "[backup-skchat] FATAL: $*" >&2; exit 1; }

# --- 1. Collect source paths that actually exist ---------------------------

TAR_ARGS=()
found_any=0

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
TMP_TAR="$(mktemp "${BACKUP_DIR}/.skchat-backup-plain-XXXXXX.tar.gz")"
chmod 600 "$TMP_TAR"

tar -czf "$TMP_TAR" "${TAR_ARGS[@]}"
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
