# Sovereign coturn: TLS on :443

This directory ships the repo-tracked, TLS-capable coturn config for skchat's
sovereign TURN relay. The goal: make the sovereign coturn the **primary** relay
for off-tailnet (cellular) callers and stop depending on the free public
`openrelay.metered.ca`.

Why TLS on 443: restrictive mobile / corporate networks routinely block UDP and
high TCP ports, which is exactly where plain TURN (3478) lives. TURN over TLS on
443/tcp looks like ordinary HTTPS and gets through almost everywhere, so a phone
on cellular can always reach the relay.

## Files

| File | Purpose |
|------|---------|
| `turnserver.conf` | Config-file form of the relay. Listens on 3478 (udp+tcp) AND `tls-listening-port=443` with `cert=` / `pkey=` pointing at the tailscale-issued cert. Template: fill/mount, never commit the secret. |
| `start-coturn.sh` | Docker launcher (TLS-capable). Adds the 443 listener + mounts the cert dir read-only. Repo equivalent of the live `~/.skchat/coturn/start-coturn.sh` and `systemd/coturn/start-coturn.sh`. |

The static-auth-secret is **never** written into these files. `start-coturn.sh`
reads it from `~/.skchat/coturn/coturn.secret` (0600) and passes it as
`--static-auth-secret`. `connectivity.py` derives short-lived per-call HMAC
credentials against the same secret (`use-auth-secret`), so no long-lived
credential ever leaves the host.

## TLS cert: issue with tailscale

Tailscale can mint a real, publicly-trusted (LetsEncrypt) cert for the node's
MagicDNS name. That name (`noroc2027.tail204f0c.ts.net`) is also the coturn
`realm`, so browsers validate the TURN/TLS hostname cleanly.

```bash
# One-time: make sure HTTPS certs are enabled for the tailnet
#   (Tailscale admin console -> DNS -> Enable HTTPS Certificates).

# Issue the cert into the dir start-coturn.sh mounts (default ~/.skchat/coturn/certs):
mkdir -p ~/.skchat/coturn/certs
tailscale cert \
  --cert-file ~/.skchat/coturn/certs/noroc2027.tail204f0c.ts.net.crt \
  --key-file  ~/.skchat/coturn/certs/noroc2027.tail204f0c.ts.net.key \
  noroc2027.tail204f0c.ts.net

# coturn (as UID inside the container) must be able to READ both files.
chmod 640 ~/.skchat/coturn/certs/noroc2027.tail204f0c.ts.net.*
```

`start-coturn.sh` mounts that directory at `/etc/coturn/certs:ro` and points
coturn at `noroc2027.tail204f0c.ts.net.crt` / `.key`.

## Rotation

Tailscale certs are short-lived (~90 days). Renew before expiry and bounce the
container so coturn reloads the new key:

```bash
# Re-issue (same command; tailscale renews in place):
tailscale cert \
  --cert-file ~/.skchat/coturn/certs/noroc2027.tail204f0c.ts.net.crt \
  --key-file  ~/.skchat/coturn/certs/noroc2027.tail204f0c.ts.net.key \
  noroc2027.tail204f0c.ts.net

# Reload: systemd owns the container lifecycle.
systemctl --user restart skchat-coturn.service
```

Automate it with a timer that renews when the cert is within ~20 days of expiry,
then restarts `skchat-coturn.service`. `tailscale cert` is idempotent, so a
periodic re-run is safe. Redundancy mantra applies: verify the new cert loads
(TURN/TLS handshake on 443) before assuming the rotation took.

## Verify the TLS listener

```bash
# TLS handshake against the 443 TURN listener (should present the tailscale cert):
openssl s_client -connect noroc2027.tail204f0c.ts.net:443 -servername noroc2027.tail204f0c.ts.net </dev/null 2>/dev/null | openssl x509 -noout -subject -dates

# Container port bindings (host networking -> host :443 + :3478):
docker exec skchat-coturn ss -tlnp 2>/dev/null | grep -E ':443|:3478' || true
```

## Wiring into ICE

Once the relay serves TLS, point the webui env at both forms (see
`deploy/env-templates/webui-*.env.example`):

```
SKCHAT_TURN_URLS=turn:noroc2027.tail204f0c.ts.net:443?transport=tls,turn:noroc2027.tail204f0c.ts.net:3478?transport=udp
SKCHAT_TURN_SECRET=<from skvault / coturn.secret>   # names-only in the template
```

With `SKCHAT_TURN_SECRET` + `SKCHAT_TURN_URLS` set, `connectivity.py` emits ONLY
the sovereign relay off-tailnet. `openrelay.metered.ca` is now opt-in last-resort
only, gated behind `SKCHAT_ALLOW_OPENRELAY` (default off) and alert-on-use.

## Live deploy note

Do NOT apply this from a worktree against the running .158 coturn. Deploy
deliberately: issue the cert, then let `systemd/install.sh` reconcile the unit,
then `systemctl --user restart skchat-coturn.service`.
