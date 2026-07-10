#!/usr/bin/env bash
# install.sh: install skchat systemd user units.
#
# Usage:
#   ./systemd/install.sh [--start] [--no-enable] [--agent NAME ...]
#
# Options:
#   --start        Start units immediately after installing.
#   --no-enable    Install only, do not enable anything.
#   --agent NAME   Enable (and with --start, start) a Telegram bridge
#                  instance skchat-telegram@NAME.service. Repeatable,
#                  e.g. --agent opus --agent lumina.
#
# The script copies unit files into ~/.config/systemd/user/, retires the
# obsolete pre-template units if a previous install left them behind,
# verifies each installed unit with systemd-analyze, and reloads the
# systemd user daemon. Run without root.
#
# Portability: no absolute /home/<user> paths and no username assumptions.
# The units resolve everything through %h, %i, and per-machine environment
# files referenced by name:
#   ~/.config/skchat/daemon.env            optional daemon overrides
#   ~/.config/skchat/telegram-<agent>.env  required per bridge instance
#                                          (must set SKC_BRIDGE_TOKEN)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
START=0
ENABLE=1
AGENTS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --start)     START=1; shift ;;
        --no-enable) ENABLE=0; shift ;;
        --agent)
            [[ $# -ge 2 ]] || { echo "error: --agent requires a name" >&2; exit 1; }
            AGENTS+=("$2"); shift 2 ;;
        *)
            echo "error: unknown option: $1" >&2; exit 1 ;;
    esac
done

UNITS=(
    skchat-daemon.service
    skchat-telegram@.service
)

# Retired units from the pre-template layout (dead ExecStart paths).
OBSOLETE_UNITS=(
    skchat-opus-bridge.service
    skchat-lumina-bridge.service
    skchat-bridges.target
)

echo "Installing skchat systemd units to ${UNIT_DIR}/"
mkdir -p "${UNIT_DIR}"

for unit in "${UNITS[@]}"; do
    src="${SCRIPT_DIR}/${unit}"
    dst="${UNIT_DIR}/${unit}"
    if [[ ! -f "$src" ]]; then
        echo "  [SKIP] ${unit} (source not found)"
        continue
    fi
    cp "${src}" "${dst}"
    echo "  [OK]   ${unit}"
done

for unit in "${OBSOLETE_UNITS[@]}"; do
    if [[ -f "${UNIT_DIR}/${unit}" ]]; then
        systemctl --user disable --now "${unit}" 2>/dev/null || true
        rm -f "${UNIT_DIR}/${unit}"
        echo "  [GONE] ${unit} (obsolete, removed)"
    fi
done

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "Verifying units..."
for unit in "${UNITS[@]}"; do
    # Templates are verified as a throwaway instance so %i resolves.
    verify_name="${unit/@./@verifyinstance.}"
    if systemd-analyze --user verify "${verify_name}"; then
        echo "  [PASS] ${unit}"
    else
        echo "  [FAIL] ${unit} failed systemd-analyze verify" >&2
        exit 1
    fi
done

if [[ $ENABLE -eq 1 ]]; then
    echo "Enabling units..."
    systemctl --user enable skchat-daemon.service
    for agent in "${AGENTS[@]}"; do
        env_file="${HOME}/.config/skchat/telegram-${agent}.env"
        if [[ ! -f "$env_file" ]]; then
            echo "  [WARN] ${env_file} missing; skchat-telegram@${agent} will not start until it exists (needs SKC_BRIDGE_TOKEN)."
        fi
        systemctl --user enable "skchat-telegram@${agent}.service"
    done
fi

if [[ $START -eq 1 ]]; then
    echo "Starting skchat-daemon..."
    systemctl --user start skchat-daemon.service
    for agent in "${AGENTS[@]}"; do
        echo "Starting skchat-telegram@${agent}..."
        systemctl --user start "skchat-telegram@${agent}.service"
    done
fi

echo ""
echo "Done. Useful commands:"
echo "  systemctl --user status skchat-daemon"
echo "  systemctl --user status skchat-telegram@<agent>"
echo "  journalctl --user -u skchat-daemon -f"
echo "  journalctl --user -u skchat-telegram@<agent> -f"
