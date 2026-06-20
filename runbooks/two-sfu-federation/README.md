# Shape B — two-SFU peer federation (lumina@.158 ⇄ jarvis@.41)

Each sovereign instance hosts conferences on its **own** LiveKit SFU; a peer joins a
remote-hosted conf via the capauth-signed cross-realm mint (`/conf/{room}/federated-token`,
client = `skchat/conf/fed_client.py`). This is the "then 2." of the SKChat Unified Client +
Federation epic (`skchat-unify`); Shape A (shared SFU on .158) is in
`runbooks/cross-instance-call-test/`.

## Deployed (2026-06-20)
- **.41 LiveKit SFU**: `livekit-server` 1.9.1, `~/.config/livekit/livekit.yaml` bound to the
  tailnet IP `100.86.156.5:7880` (rtc tcp `7881`, udp `50000-50200`, `use_external_ip:false`),
  keys `skchat-jarvis` (primary) + `skchat-lumina` (reciprocal). systemd user unit with a
  tailnet-wait `ExecStartPre`. Stand it up with `setup-jarvis-sfu.sh`.
- **jarvis webui** points at its own SFU: `webui-jarvis.env` →
  `SKCHAT_LIVEKIT_URL=ws://100.86.156.5:7880`, key `skchat-jarvis`. It advertises the public SFU
  url as `wss://cbrd21-laptop12thgenintelcore.tail204f0c.ts.net/livekit-ws` (tailscale serve).
- **tailscale serve**: `/livekit-ws → http://100.86.156.5:7880` on the .41 funnel host.

## The networking reality (important)
`.41` sits behind **two** inbound gates, so a remote peer reaching its SFU is non-trivial:

1. **PIA VPN killswitch** (`piavpn.INPUT` iptables) — drops inbound that isn't explicitly
   allowed. tailscale's own `ts-input` chain already `ACCEPT`s everything on `-i tailscale0`,
   so tunneled tailnet traffic survives; `setup-jarvis-sfu.sh` additionally pins explicit
   ACCEPTs for `7880/7881/50000-50200` so the intent is durable across PIA reloads.
2. **Tailscale ACL** (the tailnet policy) — `.41` is a **`tagged-devices`** node, and the
   tailnet ACL gates **direct peer→port** access. This is the actual blocker observed:
   - `tailscale ping .41` works (disco, not ACL-gated)
   - `https://…ts.net/livekit-ws` works (Funnel, served by the node, not peer-ACL-gated)
   - `curl http://100.86.156.5:7880` from .158 → **fails** (`EHOSTUNREACH`) — the ACL denies it.

### REQUIRED VPN/ACL rule (tailnet admin — Chef)
Add to the policy at <https://login.tailscale.com/admin/acls> so peers can reach the .41 SFU
media ports directly over the tailnet:
```json
{ "action": "accept",
  "src": ["100.108.59.57"],
  "dst": ["100.86.156.5:7880,7881,50000-50200"] }
```
Use tags instead of bare IPs if preferred (e.g. `"src":["tag:sknode"]`). Add the symmetric
grant for any other peer that must join jarvis-hosted conferences. **Signaling already works;
this rule is purely for the WebRTC media path.**

## Verify (after the ACL lands)
1. Direct reachability from .158: `curl -m6 http://100.86.156.5:7880/` should connect (not 000).
2. Two-browser call **hosted on .41's SFU**: copy `runbooks/cross-instance-call-test/` and set
   `SFU = wss://cbrd21-laptop12thgenintelcore.tail204f0c.ts.net/livekit-ws`, mint both tokens
   against the jarvis instance (one direct, one for lumina via the cross-realm
   `/conf/{room}/federated-token`), confirm both tabs see each other's video.
3. The cross-realm mint itself is unit-proven in `tests/test_conf_fed_client.py`.

## Fallbacks if the ACL can't be opened
- **Shape A (shipped, works today)**: both instances join the **.158** SFU; jarvis mints against
  it. See `runbooks/cross-instance-call-test/` — video both ways, no .41 inbound needed.
- **coturn TURN relay on .158** (conf-calls tasks `d5b00d43`/`0f70eeda`): a relay on the
  reachable host lets NAT'd/firewalled peers exchange media without opening .41 inbound. This is
  the general-purpose answer for public (non-tailnet) guests too.

## Files
- `setup-jarvis-sfu.sh` — idempotent .41 SFU bring-up (config + unit + serve + iptables + ACL note).
