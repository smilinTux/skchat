#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# deploy/stacks/make-secrets.sh — create the B3 Docker secrets for skchat-stack.yml
# ═══════════════════════════════════════════════════════════════════════════════
#
# Reads a GITIGNORED env file (default: deploy/stacks/.env.secrets, templated from
# deploy/stacks/secrets.example) and creates each value as a `docker secret`.
# Docker secrets live in the Swarm raft log (encrypted at rest) and are mounted at
# /run/secrets/<name> (tmpfs, root-only) inside the consuming containers — they are
# NEVER baked into an image layer and NEVER written to the stack YAML.
#
# Usage:
#   cp deploy/stacks/secrets.example deploy/stacks/.env.secrets
#   $EDITOR deploy/stacks/.env.secrets          # fill in every value
#   chmod 600 deploy/stacks/.env.secrets
#   deploy/stacks/make-secrets.sh               # create the secrets
#   deploy/stacks/make-secrets.sh --recreate    # rotate: rm + recreate each secret
#   ENV_FILE=/path/to/other.env deploy/stacks/make-secrets.sh
#
# Rotation: Docker secrets are immutable.  --recreate removes then recreates each
# secret; you MUST then `docker service update --force <svc>` (or redeploy the
# stack) so tasks pick up the new value.  A secret that is in use by a running
# service cannot be removed — scale/redeploy first, or use versioned secret names.
#
# Exit non-zero if any required value is empty.
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env.secrets}"
RECREATE=0
[[ "${1:-}" == "--recreate" ]] && RECREATE=1

# ── map: env var name → docker secret name ─────────────────────────────────────
# (order preserved; keep in sync with secrets.example + skchat-stack.yml)
declare -a PAIRS=(
  "LIVEKIT_API_KEY:livekit_api_key"
  "LIVEKIT_API_SECRET:livekit_api_secret"
  "TURN_SECRET:turn_secret"
  "SKMEMORY_PG_PASSWORD:skmemory_pg_password"
  "VOICE_FALLBACK_URL:voice_fallback_url"
)

# ── preflight ─────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found" >&2; exit 1; }

if ! docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null | grep -q active; then
  echo "ERROR: this node is not an active Swarm manager." >&2
  echo "       Run on the Swarm manager (docker swarm init / join as manager)." >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: secret env file not found: $ENV_FILE" >&2
  echo "       cp deploy/stacks/secrets.example deploy/stacks/.env.secrets && edit it." >&2
  exit 1
fi

# Permissions sanity (warn, don't fail).
perms="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo '???')"
[[ "$perms" =~ ^6[04]0$ || "$perms" == "600" ]] || \
  echo "WARNING: $ENV_FILE is mode $perms — recommend chmod 600." >&2

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

# ── validate all required values are present ───────────────────────────────────
missing=()
for pair in "${PAIRS[@]}"; do
  var="${pair%%:*}"
  if [[ -z "${!var:-}" ]]; then missing+=("$var"); fi
done
if (( ${#missing[@]} > 0 )); then
  echo "ERROR: empty required secret(s) in $ENV_FILE:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
fi

# ── create (or recreate) each secret ───────────────────────────────────────────
created=0; skipped=0
for pair in "${PAIRS[@]}"; do
  var="${pair%%:*}"; name="${pair##*:}"; value="${!var}"

  if docker secret inspect "$name" >/dev/null 2>&1; then
    if (( RECREATE )); then
      echo "rotate: removing existing secret '$name' ..."
      if ! docker secret rm "$name" >/dev/null 2>&1; then
        echo "ERROR: cannot remove '$name' (in use by a running service?)." >&2
        echo "       Scale/redeploy the consumer first, or use versioned names." >&2
        exit 1
      fi
    else
      echo "skip:   secret '$name' already exists (use --recreate to rotate)."
      skipped=$((skipped+1)); continue
    fi
  fi

  printf '%s' "$value" | docker secret create "$name" - >/dev/null
  echo "create: docker secret '$name' created."
  created=$((created+1))
done

echo
echo "Done. created=$created skipped=$skipped"
echo "Verify:  docker secret ls"
echo "Next:    docker stack config -c deploy/stacks/skchat-stack.yml >/dev/null   # validate"
echo "Then (supervised):  docker stack deploy --env-file /var/data/deploy_skchat/skchat.env -c deploy/stacks/skchat-stack.yml skchat"
if (( RECREATE && created > 0 )); then
  echo
  echo "ROTATED secrets — force the consumers to pick up new values:"
  echo "  docker service update --force skchat_sk-access skchat_daemon skchat_skcomms-api skchat_voice"
fi
