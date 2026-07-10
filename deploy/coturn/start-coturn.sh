#!/usr/bin/env bash
# deploy/coturn/start-coturn.sh: sovereign skchat coturn WITH TLS on :443.
#
# Repo-tracked, TLS-capable equivalent of the live ~/.skchat/coturn/start-coturn.sh
# and systemd/coturn/start-coturn.sh. It adds a TURN-over-TLS listener on 443/tcp
# and mounts the tailscale-issued cert so off-tailnet (cellular) joiners can reach
# the relay on the one port restrictive networks almost never block.
#
# Host networking is used, so 3478/udp+tcp and 443/tcp are published on the host
# directly (no -p mapping needed). Ensure nothing else already binds host :443.
#
# DO NOT run this against the live .158 coturn from a worktree. It is a reference
# for a deliberate deploy: systemd (skchat-coturn.service) owns the container
# lifecycle, so the container runs `--restart no` (systemd's Restart= supervises).
#
# Secret + cert are read from files (0600 / mounted ro), never committed:
#   COTURN_SECRET_FILE   default %h/.skchat/coturn/coturn.secret
#   COTURN_CERT_DIR      default %h/.skchat/coturn/certs   (holds <name>.crt/.key)
#   COTURN_CERT_NAME     default noroc2027.tail204f0c.ts.net
#   COTURN_REALM         default noroc2027.tail204f0c.ts.net
#   COTURN_TLS_PORT      default 443
# See deploy/coturn/README.md for cert issuance + rotation.
set -euo pipefail

SECRET_FILE="${COTURN_SECRET_FILE:-${HOME}/.skchat/coturn/coturn.secret}"
REALM="${COTURN_REALM:-noroc2027.tail204f0c.ts.net}"
CERT_DIR="${COTURN_CERT_DIR:-${HOME}/.skchat/coturn/certs}"
CERT_NAME="${COTURN_CERT_NAME:-noroc2027.tail204f0c.ts.net}"
TLS_PORT="${COTURN_TLS_PORT:-443}"

SECRET="$(cat "${SECRET_FILE}" 2>/dev/null || echo "")"
if [ -z "${SECRET}" ]; then
  echo "ERROR: coturn secret not found or empty at ${SECRET_FILE}" >&2
  echo "       provision it from systemd/coturn/coturn.secret.example (chmod 600)" >&2
  exit 1
fi

if [ ! -f "${CERT_DIR}/${CERT_NAME}.crt" ] || [ ! -f "${CERT_DIR}/${CERT_NAME}.key" ]; then
  echo "ERROR: TLS cert/key missing in ${CERT_DIR} (${CERT_NAME}.crt / ${CERT_NAME}.key)" >&2
  echo "       issue it with tailscale (see deploy/coturn/README.md):" >&2
  echo "         tailscale cert \\" >&2
  echo "           --cert-file ${CERT_DIR}/${CERT_NAME}.crt \\" >&2
  echo "           --key-file  ${CERT_DIR}/${CERT_NAME}.key \\" >&2
  echo "           ${CERT_NAME}" >&2
  exit 1
fi

exec /usr/bin/docker run -d \
  --name skchat-coturn \
  --restart no \
  --network host \
  --label skmanaged=coturn \
  -v "${CERT_DIR}:/etc/coturn/certs:ro" \
  coturn/coturn:4.6 \
  -n --log-file=stdout \
  --realm="${REALM}" \
  --use-auth-secret \
  --static-auth-secret="${SECRET}" \
  --listening-port=3478 \
  --tls-listening-port="${TLS_PORT}" \
  --cert="/etc/coturn/certs/${CERT_NAME}.crt" \
  --pkey="/etc/coturn/certs/${CERT_NAME}.key" \
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
