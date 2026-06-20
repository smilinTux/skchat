#!/usr/bin/env bash
# align_federation_fqid.sh — coord F0-fqid (84fb38da)
#
# Make ~/.skchat/federation-{trust.json,peers} consistent with the canonical
# FQID form <agent>@<operator>.<realm> (e.g. lumina@chef.skworld), so that
# cross-realm conf-token mint verifies: every trusted signer's pubkey is pinned
# under its canonical FQID filename, matching what resolve_agent_identity().fqid
# emits at sign time.
#
# Idempotent. Safe to run on .158 and .41. Only copies a pin when absent or
# changed. Never touches private keys.
set -euo pipefail

SKCAP_HOME="${SKCAPSTONE_HOME:-$HOME/.skcapstone}"
SKCHAT_HOME="${SKCHAT_HOME:-$HOME/.skchat}"
CLUSTER="$SKCAP_HOME/cluster.json"
PEERS_DIR="$SKCHAT_HOME/federation-peers"
TRUST="$SKCHAT_HOME/federation-trust.json"

log() { printf '[align-fqid] %s\n' "$*"; }

if [[ ! -f "$CLUSTER" ]]; then
  echo "[align-fqid] ERROR: cluster.json not found at $CLUSTER" >&2
  exit 1
fi

# operator + realm define the canonical FQID suffix (.<operator>.<realm>)
read -r OPERATOR REALM < <(
  python3 - "$CLUSTER" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
print(c.get("operator", ""), c.get("realm", ""))
PY
)
if [[ -z "$OPERATOR" || -z "$REALM" ]]; then
  echo "[align-fqid] ERROR: cluster.json missing operator/realm" >&2
  exit 1
fi
SUFFIX="${OPERATOR}.${REALM}"
log "canonical FQID form: <agent>@${SUFFIX}"

mkdir -p "$PEERS_DIR"

# Ensure a baseline trust file exists (canonical agents) if none present.
if [[ ! -f "$TRUST" ]]; then
  log "creating baseline $TRUST"
  cat > "$TRUST" <<JSON
{
  "full_access": ["lumina@${SUFFIX}", "opus@${SUFFIX}", "chef@${SUFFIX}", "jarvis@${SUFFIX}"],
  "default": "subscribe",
  "remote_max_role": "speaker"
}
JSON
fi

# The set of FQIDs the verifier trusts (full_access).
mapfile -t TRUSTED < <(
  python3 - "$TRUST" <<'PY'
import json, sys
t = json.load(open(sys.argv[1]))
for f in t.get("full_access", []):
    print(f)
PY
)

# For each local agent with a capauth pubkey, if its canonical FQID is trusted,
# pin its pubkey under <fqid>.asc (copy only when absent or differing).
pinned=0 skipped=0
for pub in "$SKCAP_HOME"/agents/*/capauth/identity/public.asc; do
  [[ -e "$pub" ]] || continue
  agent="$(basename "$(dirname "$(dirname "$(dirname "$pub")")")")"
  fqid="${agent}@${SUFFIX}"
  # only pin trusted signers
  is_trusted=0
  for t in "${TRUSTED[@]}"; do
    [[ "$t" == "$fqid" ]] && is_trusted=1 && break
  done
  if [[ "$is_trusted" -ne 1 ]]; then
    continue
  fi
  dest="$PEERS_DIR/${fqid}.asc"
  if [[ -f "$dest" ]] && cmp -s "$pub" "$dest"; then
    skipped=$((skipped + 1))
    continue
  fi
  cp "$pub" "$dest"
  log "pinned $fqid -> $dest"
  pinned=$((pinned + 1))
done

log "done: $pinned pinned/updated, $skipped already current"
log "pins now present:"
ls -1 "$PEERS_DIR" 2>/dev/null | sed 's/^/[align-fqid]   /' || true
