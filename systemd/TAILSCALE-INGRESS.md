# Tailscale serve/funnel ingress (.158 / noroc2027)

This is the public ingress that fronts skchat, skfed, and the coturn TURNS
relay on the primary host (.158, tailnet name `noroc2027.tail204f0c.ts.net`).
It is captured here so it is reproducible on a rebuild; the live config lives
only in tailscaled's local state (`tailscale serve`/`tailscale funnel`), not
in any file this repo previously tracked.

**CODIFY-ONLY.** This document and `tailscale-ingress.sh` snapshot the live
state as of 2026-07-16. Nothing in this repo re-applies the ingress
automatically; the shared ingress is not to be touched by an automated task
(see the safety banner in `tailscale-ingress.sh`).

Source of truth used to build this table: `tailscale serve status --json`
and `tailscale funnel status --json` on .158, both read-only.

## Why this ingress exists

.158 runs one Tailscale node (`noroc2027`). That node terminates TLS for
everything below at a single HTTPS listener on `:443`, path-routing to the
right local service, plus two raw TCP legs for TURNS. Funnel is Tailscale's
mechanism to make a `tailscale serve` mapping reachable from the public
internet (not just the tailnet); every mapping below is Funnel-enabled
because every one of them needs to be reached by a client that is NOT on the
tailnet (a guest with a browser, a call peer's WebRTC client, another
federation node). If a consumer only needed tailnet-local reach, it would use
plain `tailscale serve` (no Funnel) instead, or bypass Funnel/serve entirely
and hit the tailnet IP:port directly, as most other skchat-internal traffic
already does.

## HTTPS path mappings (`:443`, one Funnel listener)

| Path | Backend | Consumer | Why public Funnel |
|------|---------|----------|--------------------|
| `/` | `http://localhost:8765` | skchat web client (Flutter web build, served by `skchat-webui@lumina.service`) | Guest invite links (`/pair`, `/invite/...`) are sent to people who are not on the tailnet; the whole web client has to be internet-reachable for those links to open at all. |
| `/daemon` | `http://127.0.0.1:9385` | skchat daemon health/API (`skchat-daemon.service`) | The web client (served from the same public origin) calls this for health/API; browser same-origin fetches from a public page need a public backend, hence the path-proxy instead of a direct tailnet call. |
| `/livekit-ws` | `http://100.108.59.57:7880` | LiveKit SFU signaling (`livekit-server.service`, WSS) | Call peers include guests / non-tailnet devices (phones on cellular, browsers with no Tailscale client); the signaling websocket has to be reachable from the public internet for sovereign cellular calling to work at all (see MEMORY.md "skchat public 443 + calling"). |
| `/.well-known/skfed/directory` | `http://localhost:9384/.well-known/skfed/directory` | skfed federation directory (`skcomms`, `:9384`) | `.well-known` federation discovery is, by definition, fetched by other federation nodes that are not on this tailnet (SKFed peers resolving `noroc2027`'s directory the way any federated protocol's well-known endpoint works). |

All four paths sit behind the **same** Funnel-enabled `:443` listener, so
there is no per-path public/tailnet-only split at the HTTP layer today: once
Funnel is on for the node's `:443`, every `tailscale serve` path handler
registered on it is internet-reachable. (Anything that should stay
tailnet-only, e.g. the raw MCP stdio surfaces, direct daemon ports for
agent-to-agent traffic, etc., simply isn't registered as a path here at all;
it's reached by tailnet IP:port instead, outside `serve`/`funnel`.)

## TCP funnel legs

| External port | Forwards to | Consumer | Why public Funnel |
|----------------|-------------|----------|--------------------|
| `tcp :8443` | `localhost:443` | coturn TURNS (TLS-over-TCP) leg, `skchat-coturn.service` | WebRTC/coturn TURN-over-TLS needs a raw TCP port reachable from the public internet so call peers behind restrictive NATs/firewalls (that block UDP or standard ports) can still relay media; this is the primary sovereign-cellular-calling TURNS port. |
| `tcp :10000` | `localhost:443` | Secondary TLS-over-TCP leg (same coturn stack) | Same rationale as `:8443`; a second public TCP port gives the TURN relay a fallback listener (some networks/ports get blocked differently), improving call connectivity odds without changing what's behind it. |

Both TCP legs forward to `localhost:443` on .158, i.e. they ride the same
local TLS terminator as the HTTPS listener above; they are not separate local
services, just separate externally-exposed ports for the same TLS-over-TCP
backend.

## Reproducing this on a rebuild

```bash
# ALWAYS confirm the plan first; do not apply blind, especially against a host
# that is not genuinely fresh (this ingress is shared: skchat + skfed + livekit + coturn).
bash systemd/tailscale-ingress.sh --dry-run

# Only after confirming every printed command is additive (no unexpected
# [DRY-RUN] lines for mappings you did not intend to change):
bash systemd/tailscale-ingress.sh
```

The script reads `tailscale serve status --json` and skips any mapping
already present with the exact same target (see "Idempotency" below), so it
is safe to re-run, but it is still a live, shared-infrastructure change: do
not run it for real as part of an unattended/automated task. Verification of
a real re-apply-and-diff against .158 is out of scope for this codify task
and is left to the controller / a human operator doing the actual rebuild.

## Idempotency: chosen approach and why

`tailscale-ingress.sh` uses **read-and-skip**, not blind re-apply: before
running any `tailscale funnel` command it reads the current
`tailscale serve status --json` and skips a mapping if that exact path/port
already forwards to that exact target. This was chosen over relying on
tailscale's own declarative idempotency (identical `tailscale funnel`
re-invocations are *believed* to be a config-map overwrite, hence a no-op,
on tailscale 1.98.4) because that belief was not verified against a live
re-apply on .158 (out of scope, see CODIFY-ONLY above). Read-and-skip makes
every re-run auditable (`[OK] ... already configured, skipping` vs
`[DRY-RUN]`/`[RUN] ...`) regardless of which behavior turns out to be true,
and it degrades safely: worst case it re-issues a command tailscale itself
would have treated as a no-op anyway.

## Flags used, and what was verified vs assumed

Verified directly against `tailscale --version` (1.98.4) `--help` output on
.158:

- `tailscale serve --help` / `tailscale funnel --help` both expose `--bg`,
  `--set-path`, `--tcp`, `--yes` (among others); `funnel` and `serve` take
  the same flag set, `funnel` additionally flips the port's `AllowFunnel` to
  true.
- `tailscale serve get-config --all` / `set-config` exist but are for
  Tailscale's separate **Services** feature (nothing under `--service`/
  `--all` came back for this node; it returned `{"version": "0.0.1"}` with
  no handlers), not for the classic node-level `serve`/`funnel` config this
  host actually uses. **Not used here** for that reason.

Assumed (not verified against a live re-apply, since that was out of scope):

- `tailscale funnel --bg --yes --set-path <path> <url>` is the single
  command that both registers the Web path handler **and** ensures
  `AllowFunnel` is true for `:443` (i.e. using `funnel` instead of `serve`
  for these paths is both correct and sufficient, no separate "flip Funnel
  on for the port" command is needed). This matches all 4 live mappings
  showing up simultaneously under both `tailscale serve status` and
  `tailscale funnel status` with the port marked `(Funnel on)`.
  Alternative if this turns out wrong: `tailscale serve --bg --set-path
  <path> <url>` per path, followed by one `tailscale funnel --bg --yes 443`
  (or `tailscale funnel 443 on`, an older syntax not shown in this version's
  `--help`) to flip `AllowFunnel` for the port as a separate step.
- `tailscale funnel --bg --yes --tcp <port> tcp://localhost:443` is the
  right target syntax for the TCP legs (`tcp://host:port`, matching the
  `TCPForward` value from `tailscale serve status --json` verbatim). The
  `--yes` flag is included defensively, to skip the interactive Funnel
  consent prompt tailscale shows the first time a port is exposed publicly,
  since this script is meant to run unattended; it should be a no-op if no
  prompt would have appeared.

Both assumptions are marked in `tailscale-ingress.sh --dry-run` output as
plain printed commands (never executed by this task), and the printed plan
was confirmed to reproduce the live `tailscale serve status --json` mapping
set one-for-one (see `task-4-report.md`). A human operator doing an actual
rebuild should treat these two bullet points as the first thing to check if
`tailscale-ingress.sh` (run for real, not `--dry-run`) doesn't converge to
the same `tailscale serve status --json` as this document's snapshot.

## Raw snapshot (`tailscale serve status --json`, 2026-07-16)

```json
{
  "TCP": {
    "10000": { "TCPForward": "localhost:443" },
    "443": { "HTTPS": true },
    "8443": { "TCPForward": "localhost:443" }
  },
  "Web": {
    "noroc2027.tail204f0c.ts.net:443": {
      "Handlers": {
        "/": { "Proxy": "http://localhost:8765" },
        "/.well-known/skfed/directory": {
          "Proxy": "http://localhost:9384/.well-known/skfed/directory"
        },
        "/daemon": { "Proxy": "http://127.0.0.1:9385" },
        "/livekit-ws": { "Proxy": "http://100.108.59.57:7880" }
      }
    }
  },
  "AllowFunnel": {
    "noroc2027.tail204f0c.ts.net:10000": true,
    "noroc2027.tail204f0c.ts.net:443": true,
    "noroc2027.tail204f0c.ts.net:8443": true
  }
}
```
