# Cross-instance call test (Shape A — shared SFU)

Proves **lumina@.158 ↔ jarvis@.41 join the same conference and exchange video both ways**
over .158's shared LiveKit SFU. This is the B4 verification of the SKChat Unified Client +
Federation epic (`skchat-unify`).

## Topology
- **lumina** webui on `.158:8765`, LiveKit SFU on `.158` (`wss://noroc2027.tail204f0c.ts.net/livekit-ws`).
- **jarvis** webui on `.41:8765`, exposed to the tailnet via **Tailscale Funnel**
  (`https://cbrd21-laptop12thgenintelcore.tail204f0c.ts.net`) — required because `.41`'s **PIA VPN
  killswitch** (`piavpn.INPUT`) drops direct inbound on app ports; the tailscale interface is permitted.
- Both webuis mint LiveKit tokens against **.158's SFU** (jarvis's `webui-jarvis.env` carries
  `SKCHAT_LIVEKIT_URL=wss://noroc2027…:8443` + the shared `skchat-lumina` key) → Shape A "shared SFU".
  The sovereign cross-realm mint variant (`/conf/{room}/federated-token`) is proven separately by
  `tests/test_conf_fed_client.py` (B1).

## Run
```
python3 runbooks/cross-instance-call-test/drive.py
```
Mints a lumina token (loopback) + a jarvis token (via funnel, `X-Operator-Token`), serves `call.html`,
launches headless Chrome with `--use-fake-device-for-media-stream`, opens two tabs (one per identity)
both joining the same room on .158's SFU, then reads each tab's `window.__lk`.

## PASS criteria (observed 2026-06-20)
```
LUMINA: {connected:true, remotes:[{id:"jarvis",vid:true}], remoteVideo:true, published:true}
JARVIS: {connected:true, remotes:[{id:"lumina",vid:true}], remoteVideo:true, published:true}
RESULT: PASS — cross-instance call back-and-forth
```

## Gotchas
- Chrome needs internet (LiveKit client ESM from jsdelivr).
- Free ports `8099`/`9444` first with `fuser -k` (do NOT `pkill -f drive.py` — it self-matches the shell).
- jarvis's `/livekit/config` advertises `.41/livekit-ws` (its public URL) but no SFU runs on .41 yet
  (that's Shape B / `C1-sfu41`); the test points both clients at .158's SFU url explicitly.
