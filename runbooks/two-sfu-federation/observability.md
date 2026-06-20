# Federation observability — `/federation/status` + debugging a failed federated join

The read-only surface for "what does this instance's federation look like, and why
did a cross-instance (sovereign) conf join fail?". Pairs with `README.md` in this
dir (the .41 SFU bring-up + the PIA/Tailscale-ACL media-path gates).

## `GET /federation/status`

Best-effort, never-500 JSON. Hit it on the instance you're debugging
(loopback is fine):

```bash
curl -s http://127.0.0.1:8765/federation/status | jq
```

### Shape

```json
{
  "service": "skchat-federation",
  "status": "ok",
  "identity": {
    "fqid": "lumina@chef.skworld",
    "public_sfu_ws_url": "wss://noroc2027.tailXXXX.ts.net/livekit-ws",
    "public_webui_base": "https://noroc2027.tailXXXX.ts.net"
  },
  "relays": ["wss://relay.example", "..."],
  "trust": {
    "configured": true,
    "path": "/home/cbrd21/.skchat/federation-trust.json",
    "full_access": ["jarvis@chef.skworld"],
    "default": "subscribe",
    "remote_max_role": "speaker"
  },
  "pinned_peers": ["jarvis@chef.skworld"],
  "discovered_focus": [
    { "fqid": "jarvis@chef.skworld",
      "auth_url": "https://cbrd21-laptop....ts.net/conf/standup/federated-token",
      "sfu_ws_url": "wss://cbrd21-laptop....ts.net/livekit-ws" }
  ],
  "counts": {
    "live_confs": 1,
    "live_spaces": 0,
    "fed_tokens_minted": 4,
    "fed_tokens_redeemed": 2
  },
  "errors": []
}
```

| field | source | meaning |
|---|---|---|
| `identity.fqid` | `capauth.resolve_agent_identity().fqid` | who this instance signs cross-realm assertions AS |
| `identity.public_sfu_ws_url` | `SKCHAT_LIVEKIT_PUBLIC_URL` → `SKCHAT_LIVEKIT_URL` | what a remote peer dials for media |
| `identity.public_webui_base` | `SKCHAT_PUBLIC_WEBUI_URL` (or derived from the SFU host) | base of this instance's `/conf/.../federated-token` mint |
| `relays` | `SKCHAT_NOSTR_RELAYS` (comma-split) | where focus/membership discovery is published + queried |
| `trust` | `~/.skchat/federation-trust.json` (`TrustPolicy`) | per-FQID allow policy + default + remote-role cap |
| `pinned_peers` | `~/.skchat/federation-peers/*.asc` | FQIDs whose verification key is pinned (TOFU) — **stems only, key bytes are not exposed** |
| `discovered_focus` | live relay query (`FOCUS_KIND`) | focus hosts a peer's `discover_and_elect` / `/sfu/candidates` would see |
| `counts.fed_tokens_minted` | bumped in `conf/fed_client.py` | cross-realm tokens THIS instance minted FROM a remote (process-lifetime) |
| `counts.fed_tokens_redeemed` | bumped in `conf/routes.py` `/federated-token` | cross-realm tokens a remote peer redeemed AGAINST this instance |

`errors` lists any sub-source that failed (e.g. `"discovery: <relay error>"`); the
rest of the surface still renders. Counters are in-process (single-replica) and
reset on restart.

## What healthy looks like

- `identity.fqid` is non-null (capauth configured) and `public_sfu_ws_url` is the
  **public** (tailnet/Funnel) wss URL — not a bare `ws://` LAN address a remote
  peer can't reach.
- `relays` is non-empty and `discovered_focus` lists at least the peer host you
  expect to join (its `auth_url` + `sfu_ws_url` look reachable).
- `trust.default` is `subscribe` (listen-capped) or the peer is in `full_access`;
  `trust.configured` is `true`.
- For a peer you intend to verify cryptographically, its FQID appears in
  `pinned_peers`.
- After a successful join, `counts.fed_tokens_minted` (joiner) /
  `fed_tokens_redeemed` (host) increment.

## Debugging a failed federated join

A federated join (`skchat conf agent-join-federated --room R` or
`POST /conf/R/invite-agent-federated`) runs three steps —
**discover → mint → join SFU**. Map the failure to its step:

### 1. discovery empty (`DiscoveryError: no focus elected` / `discovered_focus: []`)

- `relays: []` → set `SKCHAT_NOSTR_RELAYS` on BOTH instances and restart; nothing
  is published or queryable without a relay.
- relays set but `discovered_focus` empty → the HOST never advertised. The host
  advertises on **conf create** (`advertise_conf`, C2). Confirm the host actually
  created the room AND its `SKCHAT_LIVEKIT_PUBLIC_URL` / `SKCHAT_PUBLIC_WEBUI_URL`
  are set (an incomplete advertise context is skipped — see the host's
  `errors`/logs). Cross-check the host's own `/sfu/candidates` and `/conf/candidates`.
- Bypass discovery to isolate it: pass `--host <peer-auth_url>` explicitly; if the
  join then succeeds, the problem is purely discovery/advertise, not trust/media.

### 2. trust denied (`ConfAuthDenied`, HTTP 403 from the remote `/federated-token`)

- The denial is enforced at the **REMOTE** authd, so check the HOST's
  `/federation/status` → `trust`. Your FQID (this instance's `identity.fqid`) must
  resolve to non-`deny`: either listed in the host's `full_access`, or the host's
  `default` is `subscribe`/`full`.
- `replay detected` (also 403) → the assertion nonce was reused; each mint builds a
  fresh nonce, so this means a literal retry of the SAME signed body. Re-issue.
- If the host pins keys, ensure this instance's key is in the host's
  `federation-peers/` (its `pinned_peers`), and realm matches exactly
  (`lumina@chef.skworld` ≠ `lumina@evil.attacker`).

### 3. SFU unreachable (token minted, media never connects)

The token + `sfu_ws_url` came back fine but the WebRTC media path is blocked. This
is the gate documented in **`README.md` → "The networking reality"**:

- **Tailscale ACL** — `.41` is a `tagged-devices` node; direct `peer→SFU port`
  access is ACL-gated. Symptom: `tailscale ping .41` works and the Funnel URL
  works, but `curl http://100.86.156.5:7880` from .158 → `EHOSTUNREACH`. Fix: add
  the `accept src→dst:7880,7881,50000-50200` rule (README has the exact JSON; apply
  with `apply-acl.sh` / `tailnet-policy-grant.json`).
- **PIA killswitch** — `piavpn.INPUT` drops inbound not explicitly allowed;
  `setup-jarvis-sfu.sh` pins ACCEPTs for the media ports so the intent survives PIA
  reloads.
- Confirm reachability after the ACL lands:
  `curl -m6 http://100.86.156.5:7880/` should connect (not `000`).
- Fallbacks if the ACL can't open (also in README): **Shape A** (both peers join the
  reachable .158 SFU) or a **coturn TURN relay** on the reachable host.

## Related

- `README.md` — .41 SFU bring-up, PIA + Tailscale-ACL media-path gates, fallbacks.
- `tests/test_conf_fed_client.py` — the cross-realm mint, unit-proven.
- `tests/test_conf_fed_agent.py` — the federated agent-join token flow.
- `tests/test_federation_status.py` — this endpoint's shape + never-500 contract.
