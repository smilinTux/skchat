#!/usr/bin/env bash
# tailscale-ingress.sh: reproduce the live .158 (noroc2027) tailscale
# serve/funnel ingress that fronts skchat + skfed + livekit + coturn.
#
# This ingress is shared infrastructure (skchat web/daemon, the skfed
# federation directory, the LiveKit signaling leg, and the coturn TURNS
# relay all sit behind the same Funnel-enabled node), so it is NOT part of
# any single service's own install.sh. It lives here for now because
# skchat is the primary consumer (guest links, web client, daemon health).
#
# CAPTURED FROM (source of truth): `tailscale serve status --json` on .158,
# 2026-07-16. See TAILSCALE-INGRESS.md for the full mapping table and the
# why-public-vs-tailnet-only rationale for each leg.
#
# IDEMPOTENCY (chosen approach: read-and-skip, not blind re-apply):
# before issuing any `tailscale funnel` command, this script reads the
# CURRENT `tailscale serve status --json` and skips any mapping that is
# already configured with the exact same target. This was chosen over
# relying on tailscale's own declarative idempotency (re-running an
# identical `tailscale funnel` command is *believed* to be a safe no-op on
# tailscale 1.98.4, since serve/funnel config is a map keyed by path/port,
# not an append log) because that belief has not been verified against a
# live re-apply (this task is codify-only; re-applying against the live,
# shared .158 ingress was explicitly out of scope). The read-and-skip guard
# makes re-runs auditable and safe regardless of which behavior turns out
# to be true.
#
# Usage:
#   ./systemd/tailscale-ingress.sh --dry-run   print the commands that would
#                                               run, execute nothing (default
#                                               recommended way to run this)
#   ./systemd/tailscale-ingress.sh              actually apply. Only do this
#                                               on a genuinely fresh host with
#                                               no existing serve/funnel
#                                               config, or after confirming
#                                               via --dry-run that every
#                                               planned command is additive.
#
# SAFETY: this ingress is LIVE and SHARED (skchat, skfed, livekit, coturn
# all route through it). Do NOT run this script for real against .158 as
# part of an automated task. --dry-run only, unless a human operator is
# deliberately rebuilding the host from scratch.
#
# Requires: tailscale (this script was verified against 1.98.4), jq.

set -euo pipefail

DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,38p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; exit 1 ;;
    esac
done

command -v tailscale >/dev/null 2>&1 || { echo "error: tailscale not found in PATH" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "error: jq not found in PATH (required to read serve status)" >&2; exit 1; }

# Ordered so the printed plan matches `tailscale serve status` top-to-bottom:
# path -> backend, all served at the single HTTPS (:443) Funnel listener.
PATHS=("/" "/daemon" "/livekit-ws" "/.well-known/skfed/directory")
declare -A PATH_TARGET=(
    ["/"]="http://localhost:8765"
    ["/daemon"]="http://127.0.0.1:9385"
    ["/livekit-ws"]="http://100.108.59.57:7880"
    ["/.well-known/skfed/directory"]="http://localhost:9384/.well-known/skfed/directory"
)

# externally-exposed TCP port -> local backend (TLS-over-TCP legs; both
# forward to the same local :443 the coturn/TLS listener presents on).
TCP_PORTS=("8443" "10000")
declare -A TCP_TARGET=(
    ["8443"]="localhost:443"
    ["10000"]="localhost:443"
)

STATUS_JSON="$(tailscale serve status --json 2>/dev/null || echo '{}')"

run_cmd() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  [DRY-RUN] $*"
    else
        echo "  [RUN] $*"
        "$@"
    fi
}

echo "tailscale-ingress: reconciling noroc2027 (.158) funnel config (dry-run: ${DRY_RUN})"
echo ""
echo "HTTPS path mappings (single Funnel listener on :443):"
for path in "${PATHS[@]}"; do
    target="${PATH_TARGET[$path]}"
    current="$(jq -r --arg p "$path" \
        '[.Web[]?.Handlers[$p]?.Proxy] | map(select(. != null)) | .[0] // empty' \
        <<<"$STATUS_JSON")"
    if [[ "$current" == "$target" ]]; then
        echo "  [OK] ${path} -> ${target} (already configured, skipping)"
    else
        run_cmd tailscale funnel --bg --yes --set-path "$path" "$target"
    fi
done

echo ""
echo "TCP funnel legs (TLS-over-TCP, coturn TURNS + secondary TLS leg):"
for port in "${TCP_PORTS[@]}"; do
    target="${TCP_TARGET[$port]}"
    current="$(jq -r --arg p "$port" '.TCP[$p].TCPForward // empty' <<<"$STATUS_JSON")"
    if [[ "$current" == "$target" ]]; then
        echo "  [OK] tcp:${port} -> ${target} (already configured, skipping)"
    else
        run_cmd tailscale funnel --bg --yes --tcp "$port" "tcp://${target}"
    fi
done

echo ""
echo "Done (dry-run: ${DRY_RUN})."
if [[ $DRY_RUN -eq 1 ]]; then
    echo "(dry-run: nothing was executed. Compare the plan above against"
    echo " 'tailscale serve status --json' output to confirm parity.)"
fi
