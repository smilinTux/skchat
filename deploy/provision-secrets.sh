#!/usr/bin/env bash
# provision-secrets.sh: materialise every skchat EnvironmentFile (plus the
# LiveKit config and the coturn secret) from skvault into the correct paths with
# 0600 permissions. Repo-carried names-only templates in deploy/env-templates/
# hold ${skvault:...} placeholders; the sacred vault holds the values.
#
# This is the systemd-path counterpart to the Docker/Swarm secret story in
# deploy/SECRETS.md. It is non-interactive (once the vault is unlocked) and never
# prints a secret value: the render engine (deploy/render-secrets.py) resolves
# tokens in-process and writes files directly.
#
# USAGE
#   deploy/provision-secrets.sh unlock-check   # verify the vault is unlocked
#   deploy/provision-secrets.sh dry-run        # list targets + tokens, no vault
#   deploy/provision-secrets.sh check          # resolve every token, write nothing
#   deploy/provision-secrets.sh apply          # resolve + write all files (0600)
#   deploy/provision-secrets.sh apply <name>   # write just one target by short name
#
# Requirements: `skvault unlock` first (gpg-agent SEAL). Runs under the
# ~/.skenv venv python so `import skvault` resolves.
#
# Optional env:
#   SKCHAT_PROVISION_DESTDIR   prefix prepended to every target path (test sandbox)
#   SKCHAT_BRIDGE_DSN_TEMPLATE  DSN format string with a {pw} field for the scoped
#                               role (default postgresql://skchat_bridge:{pw}@localhost:5432/skmemory)
#   PY                          python interpreter (default ~/.skenv/bin/python)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TPL="$HERE/env-templates"
RENDER="$HERE/render-secrets.py"
PY="${PY:-$HOME/.skenv/bin/python}"
DEST="${SKCHAT_PROVISION_DESTDIR:-}"

# Short name -> "template|target|extra-flags". Target paths are absolute under
# $HOME; SKCHAT_PROVISION_DESTDIR prefixes them for sandbox testing.
declare -A MAP=(
  [telegram-opus]="telegram-opus.env.example|$HOME/.config/skchat/telegram-opus.env|"
  [telegram-lumina]="telegram-lumina.env.example|$HOME/.config/skchat/telegram-lumina.env|"
  [bridge-memory]="bridge-memory.env.example|$HOME/.config/skchat/bridge-memory.env|"
  [guest-token]="guest-token.env.example|$HOME/.config/skchat/guest-token.env|"
  [webui-lumina]="webui-lumina.env.example|$HOME/.config/skchat/webui-lumina.env|"
  [webui-opus]="webui-opus.env.example|$HOME/.config/skchat/webui-opus.env|"
  [webui-chef]="webui-chef.env.example|$HOME/.config/skchat/webui-chef.env|"
  [livekit]="livekit.yaml.example|$HOME/.config/livekit/livekit.yaml|"
  [coturn]="coturn.secret.example|$HOME/.skchat/coturn/coturn.secret|--strip-final-newline"
)
ORDER=(telegram-opus telegram-lumina bridge-memory guest-token \
       webui-lumina webui-opus webui-chef livekit coturn)

die() { echo "provision-secrets: $*" >&2; exit 1; }

unlock_check() {
  "$PY" - <<'PY'
import sys
try:
    from skvault import vault_creds as vc
except Exception as e:
    print(f"skvault not importable: {e}", file=sys.stderr); sys.exit(1)
s = vc.status()
if not s.get("vault_unlocked"):
    print("vault is LOCKED (run `skvault unlock` first)", file=sys.stderr); sys.exit(1)
print("vault unlocked")
PY
}

render_one() {
  local name="$1" mode="$2"  # mode: apply|check|dry-run
  local spec="${MAP[$name]:-}"
  [ -n "$spec" ] || die "unknown target: $name (known: ${ORDER[*]})"
  local tpl target flags
  IFS='|' read -r tpl target flags <<<"$spec"
  target="${DEST}${target}"
  local args=(--template "$TPL/$tpl" --target "$target")
  [ -n "$flags" ] && args+=($flags)
  case "$mode" in
    apply)   : ;;
    check)   args+=(--check) ;;
    dry-run) args+=(--dry-run) ;;
  esac
  "$PY" "$RENDER" "${args[@]}"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  unlock-check) unlock_check ;;
  dry-run|check|apply)
    [ -f "$RENDER" ] || die "render engine missing: $RENDER"
    if [ "$#" -gt 0 ]; then
      for n in "$@"; do render_one "$n" "$cmd"; done
    else
      [ "$cmd" = "dry-run" ] || unlock_check
      for n in "${ORDER[@]}"; do render_one "$n" "$cmd"; done
    fi
    ;;
  ""|-h|--help)
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *) die "unknown command: $cmd (try --help)";;
esac
