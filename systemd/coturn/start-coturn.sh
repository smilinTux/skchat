#!/usr/bin/env bash
# start-coturn.sh: launch the sovereign coturn TURN relay as a detached Docker
# container. Invoked by skchat-coturn.service (ExecStart). systemd is the single
# owner of the container lifecycle, so the container uses `--restart no`: Docker
# does NOT auto-restart it; systemd's Restart=on-failure is the only supervisor.
# This is the fix for the split-brain where the container ran with
# `--restart unless-stopped` while the systemd unit sat inactive (plan G10).
#
# The shared TURN secret is read from a file (0600, never committed):
#   default %h/.skchat/coturn/coturn.secret, override via COTURN_SECRET_FILE.
# The realm defaults to the .158 tailnet host, override via COTURN_REALM.
#
# Installed to %h/.skchat/coturn/start-coturn.sh by systemd/install.sh.
set -euo pipefail

SECRET_FILE="${COTURN_SECRET_FILE:-${HOME}/.skchat/coturn/coturn.secret}"
REALM="${COTURN_REALM:-noroc2027.tail204f0c.ts.net}"

SECRET="$(cat "${SECRET_FILE}" 2>/dev/null || echo "")"
if [ -z "${SECRET}" ]; then
  echo "ERROR: coturn secret not found or empty at ${SECRET_FILE}" >&2
  echo "       provision it from systemd/coturn/coturn.secret.example (chmod 600)" >&2
  exit 1
fi

exec /usr/bin/docker run -d \
  --name skchat-coturn \
  --restart no \
  --network host \
  --label skmanaged=coturn \
  coturn/coturn:4.6 \
  -n --log-file=stdout \
  --realm="${REALM}" \
  --use-auth-secret \
  --static-auth-secret="${SECRET}" \
  --listening-port=3478 \
  --min-port=49152 --max-port=65535 \
  --no-multicast-peers \
  --denied-peer-ip=0.0.0.0-0.255.255.255 \
  --denied-peer-ip=10.0.0.0-10.255.255.255 \
  --denied-peer-ip=100.64.0.0-100.127.255.255 \
  --denied-peer-ip=127.0.0.0-127.255.255.255 \
  --denied-peer-ip=169.254.0.0-169.254.255.255 \
  --denied-peer-ip=172.16.0.0-172.31.255.255 \
  --denied-peer-ip=192.168.0.0-192.168.255.255 \
  --denied-peer-ip=::1 \
  --denied-peer-ip=fd00:: \
  --cipher-list=HIGH --no-tlsv1 --no-tlsv1_1 \
  --fingerprint --stale-nonce=0
