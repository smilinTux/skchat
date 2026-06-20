# Shape B — two-SFU peer federation (lumina@.158 ⇄ jarvis@.41)

Each sovereign instance hosts conferences on its **own** LiveKit SFU; a peer joins a
remote-hosted conf via the capauth-signed cross-realm mint (`/conf/{room}/federated-token`,
client = `skchat/conf/fed_client.py`). This is the "then 2." of the SKChat Unified Client +
Federation epic (`skchat-unify`); Shape A (shared SFU on .158, **works today**) is in
`runbooks/cross-instance-call-test/`.

## Deployed (2026-06-20)
- **.41 LiveKit SFU**: `livekit-server` 1.9.1, `~/.config/livekit/livekit.yaml` bound to the
  tailnet IP `100.86.156.5:7880` (rtc tcp `7881`, udp `50000-50200`, `use_external_ip:false`),
  keys `skchat-jarvis` (primary) + `skchat-lumina` (reciprocal). systemd user unit with a
  tailnet-wait `ExecStartPre`. Reachable **locally** on its own tailnet bind (verified `200`).
  Stand it up with `setup-jarvis-sfu.sh`.
- **jarvis webui** points at its own SFU (`webui-jarvis.env` → `SKCHAT_LIVEKIT_URL=ws://100.86.156.5:7880`,
  key `skchat-jarvis`); advertises the public SFU url `wss://cbrd21-laptop12thgenintelcore.tail204f0c.ts.net/livekit-ws`.
- **tailscale serve**: `/livekit-ws → http://100.86.156.5:7880` (signaling).
- **Federation identity**: lumina + jarvis pinned & trusted FULL on BOTH boxes; cross-realm mint
  (`fed_client.py`) + focus-advertise/discovery (`advertise.py`) wired and unit/live-proven.

## The actual blocker: PIA killswitch on .41 (NOT the tailnet ACL)
Empirically pinned (2026-06-20):
- The tailnet **ACL is allow-all** (`grants: [{src:["*"],dst:["*"],ip:["*"]}]`) — confirmed by GET;
  adding/reverting an `acls` grant changed nothing. **The ACL is not involved.**
- `tailscale ping` .158↔.41 works (direct, 6ms); the Funnel works; ssh works.
- LiveKit answers on `.41`'s own tailnet bind (`curl http://100.86.156.5:7880` **on .41** → `200`).
- But `.158 → 100.86.156.5:7880` → **`EHOSTUNREACH`/`000`**.

⇒ The drop is **.41's PIA VPN** (`piavpn.INPUT` + PIA mangle/`MARK` rules + `protectLoopback`).
PIA's killswitch treats inbound tailnet peer traffic to the SFU as non-VPN and blocks it (and/or
hijacks the reply out the PIA tunnel → asymmetric). tailscale's own `ts-input` ACCEPTs `-i tailscale0`,
and adding plain `INPUT` ACCEPTs did **not** fix it — the interference is in PIA's chains, which should
not be hand-edited blind.

### Fix options for the .41 inbound-media path (pick one)
1. **PIA app config (recommended, host-level):** allow the tailscale interface / enable split-tunnel so
   PIA stops filtering `tailscale0`. In the PIA client: *Settings → Network → Allow LAN* and add the
   tailscale interface to bypass, or run PIA in split-tunnel excluding `tailscale0`/the tailnet CIDR
   `100.64.0.0/10`. Then `curl -m6 http://100.86.156.5:7880/` from .158 should connect.
2. **coturn TURN relay on .158 (clean, PIA-agnostic):** a relay on the reachable host lets the .41 SFU
   AND remote clients gather a relay candidate on .158 (both reach it *outbound*), so media flows
   `.158-client ↔ .158-coturn ↔ .41-SFU` with **zero .41 inbound**. This is conf-calls tasks
   `d5b00d43`/`0f70eeda` and is also the answer for public (non-tailnet) guests.
3. **Shape A (works now):** both instances join the **.158** SFU; jarvis mints against it. No .41
   inbound needed. See `runbooks/cross-instance-call-test/`. Use this until Shape-B media is unblocked.

## Verify (after option 1 or 2)
1. From .158: `curl -m6 http://100.86.156.5:7880/` connects (not `000`).
2. Two-browser call **hosted on .41's SFU**: copy `runbooks/cross-instance-call-test/drive.py`, set
   `SFU = wss://cbrd21-laptop12thgenintelcore.tail204f0c.ts.net/livekit-ws`, mint one token direct on
   jarvis and one for lumina via the cross-realm `/conf/{room}/federated-token`; confirm both tabs see
   each other's video.
3. Cross-realm mint itself: `tests/test_conf_fed_client.py`. Discovery: `tests/test_fed_advertise.py`.

## Files
- `setup-jarvis-sfu.sh` — idempotent .41 SFU bring-up (config + unit + serve + ACCEPT rules).
