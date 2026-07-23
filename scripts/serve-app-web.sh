#!/usr/bin/env bash
# serve-app-web.sh - launch the hardened static server for the built Flutter
# web client (coord b5078963, Task 7 of skchat-resilience-v1).
#
# Thin wrapper around serve_app_web.py so the systemd unit has a single
# ExecStart target with sane defaults, all overridable by environment
# variables (the unit sets them explicitly; this script also works run by
# hand for local testing).
#
# Env vars (all optional):
#   SKCHAT_APP_WEB_PYTHON  interpreter to use (default: the repo's skenv python
#                           if present, else python3 on PATH)
#   SKCHAT_APP_WEB_ROOT    directory to serve (default: sibling skchat-app
#                           repo's build/web)
#   SKCHAT_APP_WEB_PORT    port to bind (default: 8088, matches the live unit)
#   SKCHAT_APP_WEB_BIND    bind address (default 127.0.0.1; the shipped unit
#                           sets 0.0.0.0 - :8088 is reached directly on the
#                           tailnet/LAN, not funnel-fronted)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_PARENT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ -n "${SKCHAT_APP_WEB_PYTHON:-}" ]]; then
    PYTHON="${SKCHAT_APP_WEB_PYTHON}"
elif [[ -x "${HOME}/.skenv/bin/python" ]]; then
    PYTHON="${HOME}/.skenv/bin/python"
else
    PYTHON="python3"
fi

ROOT="${SKCHAT_APP_WEB_ROOT:-${REPO_PARENT}/skchat-app/build/web}"
PORT="${SKCHAT_APP_WEB_PORT:-8088}"
BIND="${SKCHAT_APP_WEB_BIND:-127.0.0.1}"

exec "${PYTHON}" "${SCRIPT_DIR}/serve_app_web.py" --root "${ROOT}" --port "${PORT}" --bind "${BIND}"
