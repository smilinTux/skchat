#!/usr/bin/env bash
# restore-skchat.sh - decrypt and extract a skchat-backup-skchat.sh archive.
#
# Decrypts an encrypted archive (gpg or sq OpenPGP message, either works
# with either tool's decrypt since both speak standard OpenPGP) and extracts
# it into --target. Defaults to a scratch dir, NEVER the live ~/.skchat, so
# restoring over live state always requires an explicit --target.
#
# Usage:
#   restore-skchat.sh [--target DIR] <encrypted-archive-path>
#
#   --target DIR   Extraction directory. Default: a fresh mktemp dir under
#                  ${TMPDIR:-/tmp} (printed on completion). Pass the real
#                  home explicitly (e.g. --target "$HOME") to restore over
#                  live state -- this script never assumes that for you.
#
# Decryption prompts for the PGP passphrase (or uses a running gpg-agent)
# interactively; this script does not handle passphrases non-interactively.

set -euo pipefail

TARGET=""
ARCHIVE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)
            if [[ -z "$ARCHIVE" ]]; then
                ARCHIVE="$1"; shift
            else
                echo "error: unexpected argument: $1" >&2; exit 1
            fi
            ;;
    esac
done

[[ -n "$ARCHIVE" ]] || { echo "error: missing required <encrypted-archive-path>" >&2; exit 1; }
[[ -f "$ARCHIVE" ]] || { echo "error: archive not found: $ARCHIVE" >&2; exit 1; }

if [[ -z "$TARGET" ]]; then
    TARGET="$(mktemp -d "${TMPDIR:-/tmp}/skchat-restore-XXXXXX")"
fi
mkdir -p "$TARGET"

TMP_TAR=""
GPG_ERR=""
# Idempotent: safe to call repeatedly (blanks the vars after removal).
cleanup() {
    if [[ -n "$TMP_TAR" && -f "$TMP_TAR" ]]; then
        if command -v shred >/dev/null 2>&1; then
            shred -u -z "$TMP_TAR" 2>/dev/null || rm -f "$TMP_TAR"
        else
            rm -f "$TMP_TAR"
        fi
    fi
    [[ -n "$GPG_ERR" && -f "$GPG_ERR" ]] && rm -f "$GPG_ERR"
    TMP_TAR=""
    GPG_ERR=""
}
# Terminating signals must exit, not fall through to statements that
# reference the just-deleted temp file. EXIT re-runs cleanup (no-op after a
# signal handler already ran it, since cleanup is idempotent).
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
trap cleanup EXIT

log()  { echo "[restore-skchat] $*"; }
fail() { echo "[restore-skchat] FATAL: $*" >&2; exit 1; }

TMP_TAR="$(mktemp "${TMPDIR:-/tmp}/skchat-restore-plain-XXXXXX.tar.gz")"
chmod 600 "$TMP_TAR"

decrypted=0
if command -v gpg >/dev/null 2>&1; then
    GPG_ERR="$(mktemp "${TMPDIR:-/tmp}/skchat-restore-gpg-err-XXXXXX")"
    if gpg --batch --yes -o "$TMP_TAR" -d "$ARCHIVE" 2>"$GPG_ERR"; then
        decrypted=1
    fi
    rm -f "$GPG_ERR"
    GPG_ERR=""
fi

if [[ $decrypted -eq 0 ]] && command -v sq >/dev/null 2>&1; then
    if sq decrypt --output "$TMP_TAR" "$ARCHIVE"; then
        decrypted=1
    fi
fi

[[ $decrypted -eq 1 ]] || fail "could not decrypt ${ARCHIVE} with gpg or sq (need the matching private key unlocked in gpg-agent, or sq with the right key)"
[[ -s "$TMP_TAR" ]] || fail "decryption produced an empty file"

log "decrypted archive, extracting to ${TARGET}"

# The archive is encrypted but NOT signed, so treat its member paths as
# untrusted. List members first and refuse to extract if any is an absolute
# path or contains a `..` traversal component, so a crafted archive cannot
# write outside $TARGET. GNU tar additionally strips leading '/' by default
# (we do NOT pass -P/--absolute-names), which is a second layer of defense.
while IFS= read -r member; do
    [[ -z "$member" ]] && continue
    case "$member" in
        /*)
            fail "unsafe absolute path in archive member: ${member}" ;;
        *..*)
            # Reject `..` as a full path component (../, /../, trailing /..).
            case "/$member/" in
                */../*) fail "unsafe '..' traversal in archive member: ${member}" ;;
            esac
            ;;
    esac
done < <(tar -tzf "$TMP_TAR")

tar -xzf "$TMP_TAR" -C "$TARGET"

log "restore complete: ${TARGET}"
