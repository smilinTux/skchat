#!/usr/bin/env bash
# install.sh — Install skchat systemd user units
#
# Usage:
#   ./systemd/install.sh [--start] [--enable]
#
# Options:
#   --start   Start units immediately after installing
#   --enable  Enable units to start on login (default: yes for target)
#
# The script copies service files into ~/.config/systemd/user/ and reloads
# the systemd user daemon.  Run without root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
START=0
ENABLE=1

for arg in "$@"; do
    case "$arg" in
        --start)  START=1 ;;
        --no-enable) ENABLE=0 ;;
    esac
done

UNITS=(
    skchat-daemon.service
    skchat-lumina-bridge.service
    skchat-opus-bridge.service
    skchat-bridges.target
)

echo "Installing skchat systemd units to ${UNIT_DIR}/"
mkdir -p "${UNIT_DIR}"

for unit in "${UNITS[@]}"; do
    src="${SCRIPT_DIR}/${unit}"
    dst="${UNIT_DIR}/${unit}"
    if [[ ! -f "$src" ]]; then
        echo "  [SKIP] ${unit} — source not found"
        continue
    fi
    cp "${src}" "${dst}"
    echo "  [OK]   ${unit}"
done

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

if [[ $ENABLE -eq 1 ]]; then
    echo "Enabling units..."
    systemctl --user enable skchat-daemon.service
    systemctl --user enable skchat-bridges.target
    systemctl --user enable skchat-lumina-bridge.service
    systemctl --user enable skchat-opus-bridge.service
fi

if [[ $START -eq 1 ]]; then
    echo "Starting skchat-daemon..."
    systemctl --user start skchat-daemon.service
    echo "Starting skchat-bridges.target..."
    systemctl --user start skchat-bridges.target
fi

echo ""
echo "Done.  Useful commands:"
echo "  systemctl --user status skchat-daemon"
echo "  systemctl --user status skchat-bridges.target"
echo "  journalctl --user -u skchat-daemon -f"
echo "  journalctl --user -u skchat-lumina-bridge -f"
echo "  journalctl --user -u skchat-opus-bridge -f"
