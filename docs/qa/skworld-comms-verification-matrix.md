# SKWorld Comms — Verification Matrix

**Living QA document.** Maps every built component to the test cases that verify it,
the integration groups that verify components working *together*, and the real
end-to-end use cases a human/agent actually performs. Updated as features integrate.

**Scope:** `skchat` (the app/interface trunk; sprint waves 2–6 merged to `main`,
`f4acfa1`→`bdb83ce`) + `skcomms` (transport + identity + glossa + channel adapters,
merged to `main` `8ecffe8`).

**Baseline (2026-06-13):** skchat = **1082 tests / 89 files** · skcomms unified =
**497 passing / 57 files**. Both green.

**Baseline (2026-06-17):** skchat = **~1829 tests collected / 106 test files**
(grew well past the 1082 baseline across waves 2–6: hardening + AdapterHub +
federation discovery + glossa gatekeeper/session-daemon + U14 reply-routing/bind +
voice Conversation/VoiceSession + preflight harnesses) · skcomms unified =
**595 tests collected** (`8ecffe8`, SKAGENT per-agent wire identity + AdapterRegistry
instantiated). Both green. skchat main `f4acfa1`→`bdb83ce`; all pushed to GitHub.

---

## How to read the verification level

| Level | Meaning |
|---|---|
| **CI** | Automated test exists and passes. Logic verified; no live infra. |
| **CI-int** | Cross-component integration test (still in-process, no external services). |
| **LIVE ✅** | Actually run end-to-end on real infra (.158/.41/phone) and observed working. |
| **LIVE ⏳** | Built + CI-green, but **not yet run live**. Pending Tier 5 QA pass. |
| **GATED** | Cannot be live-verified here — needs Chef's creds (bot tokens) or hardware (BLE/LoRa radio, second SFU host, a phone). |

The honesty rule for this repo: **CI-green ≠ done.** A component is only "done" when its
real use case has a **LIVE ✅**.

---

## 1. Component test inventory

### 1a. skchat — messaging core

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Transport (send/recv over skcomms) | `test_transport.py` | 19 | ChatTransport send/receive, retsplit, errors | CI |
| Message history (SQLite) | `test_history.py` | 18 | persist, query, thread grouping | CI |
| Reliable outbox | `test_outbox.py` | 23 | retry/backoff, idempotent delivery | CI |
| Models | `test_models.py` | 27 | ChatMessage/Group/Peer/MessageType validation | CI |
| Ephemeral channels | `test_ephemeral.py` | 18 | TTL auto-delete, no-persist | CI |
| Reactions | `test_reactions.py` | 19 | add/remove/list emoji on messages | CI |
| 3-way chat | `test_3way_chat.py` | 26 | multi-party routing | CI-int |
| Presence/typing | `test_presence.py` | 37 | online/offline, typing indicators | CI |

### 1b. skchat — agents, daemon, identity

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Advocacy (`@mention` → AI reply) | `test_advocacy.py` | 27 | mention detect, route to skcapstone, in-thread reply | CI |
| AdapterHub (inbound bridge → chat) | `adapter_hub.py`, `test_adapter_hub.py` | 40 | inbound `ChannelMessage`→`ChatHistory`+advocacy, FQID resolve, UNTRUSTED fallback (Wave 4) | CI |
| Reply-routing + `/bind` (U14 outbound) | `adapter_bind.py`, `test_adapter_bind*.py` | 82 | `adapter.send` reply back to channel; `/bind` CapAuth; restart-durable `FqidBindingStore` (Wave 5) | CI |
| Agent comm primitives | `test_agent_comm.py` | 14 | low-level a2a messaging | CI |
| Agent profile | `test_agent_profile.py` | 13 | agent-aware identity resolution | CI |
| Identity bridge (capauth resolver) | `test_identity_bridge.py` | 17 | dual URI (capauth_uri + fqid) delegation | CI |
| Daemon loop | `test_daemon.py` | 39 | poll, dispatch, lifecycle | CI |
| Daemon integration | `test_daemon_integration.py` | 6 | daemon + transport + advocacy together | CI-int |
| Watchdog | `test_watchdog.py` | 25 | health monitor, restart triggers | CI |
| Peer discovery | `test_peer_discovery.py` | 27 | load peers from `~/.skcapstone/peers/` | CI |

### 1c. skchat — groups, pairing, files, voice

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Group chat | `test_group.py` | 38 | encrypted groups, membership, roles | CI |
| Guest links | `test_guest.py` | 37 | guest-token join, scoped access | CI |
| Pairing gate | `test_pairing_gate.py`, `test_pairing_gate_webui.py` | 11 | QR/TOFU pair, signature gate | CI |
| WebUI pair page | `test_webui_pair.py` | 9 | /pair page, call button, ring banner | CI |
| Files | `test_files.py` | 33 | file transfer | CI |
| Attachments | `test_attachments.py`, `test_webui_attachments.py` | 16 | chat attachments (web + CLI) | CI |
| Media | `test_media.py` | 5 | media handling | CI |
| Voice (Piper TTS + Whisper STT) | `test_voice.py`, `test_voice_backends.py`, `test_voice_pluggable.py` | 56 | TTS/STT, pluggable backends, `_process_speech` E2E (Wave 2) | CI |
| Voice loop (STT→LLM→TTS) | `voice_engine/voice_session.py`, `voice_engine/conversation.py` | — | `Conversation` VO threaded into `VoiceEngine.respond`; `VoiceSession` full round-trip | **LIVE ✅** (2026-06-17 — real Whisper :18794 → qwen3.5/qwen3.6 → fast Piper CPU TTS, 3.4s vs F5-TTS 113s; F-7) |
| Crypto / encrypted store | `test_crypto.py`, `test_encrypted_store.py`, `test_plugins_skseal.py` | 68 | PGP sign/verify, AES store, SKSeal | CI |
| Plugins | `test_plugins.py`, `test_plugins_skseal.py` | 99 | plugin loader, built-ins | CI |
| MCP / CLI / TUI | `test_mcp_server.py`, `test_cli.py`, `test_tui.py` | 101 | 40 MCP tools, CLI commands, TUI | CI |
| Notifications | `test_notifications.py` | 10 | notification history | CI |

### 1d. skchat — WebRTC calls (1:1)

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Call routes | `test_call_routes.py` | 16 | `/call/start` ring, `/call/answer`, `/call/incoming` sig-gate, `/call/peers` | CI |
| Call session | `test_call_session.py` | 6 | `derive_room()` per-pair, CALL_INVITE build/parse | CI |
| Connectivity (ICE ladder) | `test_connectivity.py` | 5 | Tailscale→LAN→coturn tier ladder | CI |
| Call observability | `test_call_observability.py` | 4 | call events/metrics | CI |
| Call orchestrator | `test_call_orchestrator.py` | 3 | call coordination | CI |
| P2P calls | `test_p2p_calls.py` | 4 | peer-to-peer call path | CI |
| WebRTC health | `test_webrtc_health.py` | 8 | media/ICE health checks | CI |
| Call integration | `test_call_integration.py` | 1 | end-to-end call wiring | CI-int |
| **1:1 browser call** | runbook `runbooks/browser-call-test.md` | — | live ring + join between two browsers | **LIVE ⏳** |

### 1e. skchat — SK Spaces (audio rooms)

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Space model | `test_spaces_space.py` | 3 | create, id, lifecycle | CI |
| Roles | `test_spaces_roles.py` | 4 | host/speaker/listener grants | CI |
| Tokens | `test_spaces_tokens.py` | 4 | LiveKit JWT mint per role | CI |
| Registry | `test_spaces_registry.py` | 5 | in-memory space registry, `.live()` | CI |
| Routes | `test_spaces_routes.py` | 7 | create/join/host routes | CI |
| Moderator | `test_spaces_moderator.py`, `test_spaces_moderation_routes.py` | 22 | promote/demote, mutual-consent raise-hand, host-gating | CI |
| Recording (egress) | `test_spaces_recorder.py`, `test_spaces_recording_routes.py` | 9 | audio-only egress, ● REC | CI |
| Consent ledger | `test_spaces_consent.py`, `test_spaces_consent_ledger.py` | 15 | per-speaker recording consent | CI |
| Directory (live-now) | `test_spaces_directory.py` | 2 | live space listing, XSS-escaped | CI |
| Guest join | `test_spaces_guest_join.py` | 2 | guest-link listener join | CI |
| UI markup / page | `test_spaces_ui_markup.py`, `test_spaces_page.py` | 5 | space.html render, id sanitize | CI |
| WebUI wired | `test_spaces_webui_wired.py` | 1 | routes registered into webui | CI-int |
| **Lane persistence (Tier 2)** | `test_lane_store.py`, `test_lane_dispatcher.py`, `test_lane_routes.py`, `test_lane_client_markup.py` | 17 | LaneStore snapshot/log, dispatcher validate+route, `/lanes/event`+`/lanes/{lane}/state`, client mirror+catch-up | **LIVE ✅** (all 5 lanes persist+replay live on `:8765` + unknown-lane→400; harness `scripts/tier5_verify.py`, F-3) |
| **Recording write-up (transcript→chat lane)** | `test_recording_writeup.py` | 5 | transcribe→summarize→post-to-chat-lane orchestrator; graceful no-transcript note; injectable seams | CI |
| **2-phone audio** | manual (Town Hall `space-zvteyh73i6b6czb6`) | — | two phones, one SFU, real audio | **LIVE ✅** |
| **Data-channel lane (real-time peer)** | `scripts/qa_two_browser.py` | — | two headless browsers, same Space, A `publishLane(chat)` over WebRTC data channel → B receives via `DataReceived` | **LIVE ✅** (two contexts connect to SFU + round-trip a chat-lane DC message; harness, F-6) |

### 1f. skchat — Federation (cross-host Spaces)

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Assertion (signed FQID) | `test_fed_assertion.py` | 8 | build/verify capauth-signed assertion | CI |
| authd (`/sfu/get`) | `test_fed_authd.py`, `test_fed_authd_policy.py` | 9 | verify→trust→mint, remote-role cap, space-live validation | CI |
| Trust policy | `test_fed_trust.py`, `test_fed_trust_remote_cap.py` | 8 | access_for, remote_max_role cap | CI |
| Keystore (pinned pubkey) | `test_fed_keystore.py` | 5 | realm-qualified key pinning | CI |
| Nonce (replay guard) | `test_fed_nonce.py` | 5 | NonceCache, two-sided freshness | CI |
| Focus selection | `test_fed_focus.py` | 4 | deterministic oldest-membership focus | CI |
| Events / Nostr IO | `test_fed_events.py`, `test_fed_nostr_io.py` | 12 | NIP-53 shapes, signed discovery | CI |
| `/sfu/get` route policy | `test_fed_sfu_get_policy.py` | 1 | registry-backed space-live wired | CI-int |
| Discovery client + `/sfu/candidates` | `spaces/federation/discovery.py`, `test_fed_discovery*.py` | 13 | `FederationDiscoveryClient` + `GET /sfu/candidates` over Nostr (Wave 4) | CI |
| **Cross-host token mint** | manual (opus@.41 → .158) | — | real speaker token, capped, tamper/replay→403 | **LIVE ✅** (2026-06-17 re-verified opus@.41→.158 SFU `wss://noroc2027.tail204f0c.ts.net:8443`; F-8) |
| **Client discovery / focus-election** | `/sfu/candidates` + live Nostr relay :7447 | — | `/sfu/candidates` returns the .158 host via the live discovery relay | **LIVE ✅** (2026-06-17 — `skchat-nostr-relay :7447`; F-8) |

### 1g. skchat — SKGlossa mesh (AI-to-AI language, in-app)

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Mesh bus | `test_glossa_mesh_bus.py` | 2 | MeshBus + FakeBus, on_leave seam | CI |
| Mesh protocol | `test_glossa_mesh_protocol.py` | 3 | announce/message framing | CI |
| Mesh node | `test_glossa_mesh_node.py` | 5 | group negotiation (weakest-caps), forget_peer | CI |
| Mesh integration | `test_glossa_mesh_integration.py` | 1 | 10-agent FakeBus mesh, all decode + audit-gloss | CI-int |
| LiveKit bus | `test_glossa_mesh_livekit.py` | 1 | bus over LiveKit data channel | CI |
| Modem (FSK) | `test_glossa_modem.py` | 3 | pure-Python soft-modem | CI |
| MAC (carrier-sense) | `test_glossa_mac.py` | 3 | CarrierSenseMAC + FakeAudioMedium | CI |
| Audio bus | `test_glossa_audio_bus.py`, `test_glossa_audio_mesh.py` | 4 | SKGlossa over audio, unchanged node | CI |
| Gatekeeper (capauth frame sign/verify) | `glossa_mesh/gatekeeper.py`, `test_glossa_gatekeeper.py` | 14 | `GlossaMeshGatekeeper` capauth frame sign/verify (Wave 4) | CI |
| Session daemon | `glossa_mesh/session_daemon.py`, `test_glossa_session_daemon.py` | 10 | `GlossaMeshSessionDaemon` sign outbound / verify inbound pre-decode → advocacy+memory, peer-leave (Wave 5) | CI |
| importorskip guards | (10 glossa modules) | — | optional-dep guards so mesh imports never hard-fail CI (Wave 2) | CI |
| **Live 2-agent Space mesh** | `scripts/preflight_glossa_live.py` | — | two `GlossaMeshSessionDaemon`, gatekeeper-signed frames over the live data channel | **LIVE ✅** (2026-06-17 — PASS 8/8 over the REAL SFU: signed frames, forgery rejection ×3, live peer-leave un-cap; F-9) |

### 1h. skcomms — identity, envelope, transport plumbing

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Envelope v1 | `test_envelope_v1.py` | 10 | PGP sign/verify, happy + tamper | CI |
| Identity / realm (FQID) | `test_identity_realm.py` | 13 | realm addressing, dual URI | CI |
| Capauth key reconcile | `test_capauth_key_reconcile.py` | 3 | per-agent key (not operator) | CI |
| Home scaffold | `test_home_scaffold.py` | 6 | agent home layout | CI |
| Grants (capability tokens) | `test_grants.py` | 10 | mint/verify, expiry, tamper, accept-list | CI |
| Mailbox | `test_mailbox.py` | 5 | inbox read + verify | CI |
| Peers / TOFU | `test_peers.py`, `test_tofu.py` | 17 | peer dir, trust-on-first-use | CI |
| Pairing | `test_pairing.py`, `test_pairing_cli.py` | 16 | skp:// QR + TOFU pair | CI |
| Registry | `test_registry.py`, `test_registry_cli.py` | 32 | transport registry | CI |
| Signaling / P2P / broker | `test_signaling_*.py`, `test_p2p_*.py`, `test_broker_server.py` | 19 | WebRTC signaling, broker, mailbox select | CI |
| Integration / smoke | `test_integration.py`, `test_smoke.py` | 16 | cross-transport round-trips | CI-int |

### 1i. skcomms — SKGlossa (language core)

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Message IR | `test_glossa_message.py` | 2 | language-neutral IR | CI |
| Codebook | `test_glossa_codebook.py` | 4 | synthetic codes | CI |
| Codec L0/L1/L2 | `test_glossa_codec_l0/l1/l2.py` | 12 | English→CBOR→macro-lexicon | CI |
| Macros | `test_glossa_macros.py`, `test_glossa_macros_render.py`, `test_glossa_session_macros.py` | 10 | in-context macro expansion (−67% validated tier) | CI |
| Handshake | `test_glossa_handshake.py`, `test_glossa_handshake_lexicon.py` | 9 | density negotiation to weaker ceiling | CI |
| Session | `test_glossa_session.py` | 6 | session lifecycle | CI |
| Gloss / to-human | `test_glossa_gloss.py`, `test_glossa_to_human.py` | 7 | always-decodable audit invariant (en/zh/glyph) | CI |
| Emergent | `test_glossa_emergent.py`, `_protocol.py`, `_negotiator.py` | 10 | agents invent own macros mid-session, auditable | CI |

### 1j. skcomms — BLE/SMP proximity mesh

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Protocol (MeshPacket) | `test_ble_protocol.py` | 8 | Ed25519-signed packet framing | CI |
| Fragment reassembly | `test_ble_fragment.py` | 7 | fragment/reassemble, malformed-input hardened | CI |
| Identity | `test_ble_identity.py` | 6 | per-node identity, verify | CI |
| Noise_XX | `test_ble_noise.py` | 2 | encrypted session handshake | CI |
| Relay (TTL gossip) | `test_ble_relay.py` | 5 | multi-hop gossip, TTL | CI |
| Mesh | `test_ble_mesh.py` | 3 | A→C-via-B multi-hop (FakeRadio, zero Bluetooth) | CI-int |
| Radio (FakeRadio) | `test_ble_radio.py` | 3 | radio seam | CI |
| GATT | `test_ble_gatt.py` | 2 | GATT service shape | CI |
| Pairing bundle | `test_ble_pairing_bundle.py` | 2 | QR bundle carries Noise static key | CI |
| **Real Bluetooth radio** | — | — | bleak on .41/NUC — not run | **GATED** (BT hardware) |

### 1k. skcomms — LoRa transport

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Interface seam | `test_lora_interface.py` | 3 | LoRaMeshInterface + FakeLoRaInterface | CI |
| Framing | `test_lora_framing.py` | 4 | compact MeshPacket payload | CI |
| Addressing | `test_lora_addressing.py` | 6 | LoRa addressing | CI |
| Store-and-forward | `test_lora_store.py` | 4 | airtime budget, duty-cycle cap | CI |
| Transport | `test_lora_transport.py` | 4 | send path + duty-cycle wired | CI |
| Meshtastic iface | `test_lora_meshtastic_iface.py` | 2 | real-node interface | CI |
| **Real LoRa node** | — | — | no board yet | **GATED** (LoRa hardware) |

### 1l. skcomms — Channel adapters (bridges)

| Component | Test file(s) | Cases | What's verified | Level |
|---|---|---|---|---|
| Adapter base + registry | `test_channel_adapter.py` | 46 | ChannelAdapter ABC, AdapterRegistry | CI |
| Telegram | `test_telegram_adapter.py` | 43 | TG bridge (~100% code) | CI |
| Matrix | `test_matrix_adapter.py` | 61 | Matrix bridge (~70%) | CI |
| Discord | `test_discord_adapter.py` | 40 | Discord bridge (stub client) | CI |
| Slack | `test_slack_adapter.py` | 39 | Slack bridge (stub client) | CI |
| **Registry instantiated + daemon loop** | skcomms daemon lifespan + `GET /adapters` | — | `AdapterRegistry` + factory instantiated in the skcomms daemon lifespan (Wave 3a keystone); `GET /adapters` endpoint (Wave 2) | CI-int → **LIVE ✅** (2026-06-17 — registry live in the running skcomms daemon; F-8 path) |
| SKAGENT per-agent wire identity | `test_capauth_key_reconcile.py` (+ skcomms) | — | fixes the opus-transmits-as-lumina collision (per-agent wire identity) | CI |
| **Live bridge to real Telegram (`@seaBird_Opus_bot`)** | `scripts/telegram_bridge.py` (`skchat-telegram-opus.service`) | — | real Opus (opus-unhinged soul) on qwen3.6-27b-abliterated @ 32k ctx, uncensored HTML replies, per-chat memory | **LIVE ✅** (2026-06-17 — persistent service; F-7) |
| **Live bridge to real Discord/Slack/Matrix** | — | — | needs bot tokens | **GATED** (Chef creds) |

---

## 2. Integration groups (components working together)

These are the cross-component tests + live proofs that verify *seams*, not units.

| Group | Backing tests / proof | Components joined | Level |
|---|---|---|---|
| **G-MSG** Daemon ↔ transport ↔ advocacy | `test_daemon_integration.py`, `test_integration_backbone.py`, `test_integration_roundtrip.py` | daemon + transport + advocacy + history | CI-int |
| **G-E2E** End-to-end chat | `test_e2e_chat.py`, `test_e2e_live.py`, `test_e2e_claude_lumina.py`, `test_e2e_lumina.py` | full send→receive→reply across identities | CI-int |
| **G-CALL** 1:1 call wiring | `test_call_integration.py` + browser-call runbook | call routes + session + connectivity + pairing sig-gate | CI-int → **LIVE ⏳** |
| **G-SPACE** Spaces in webui | `test_spaces_webui_wired.py` + 2-phone Town Hall | spaces routes + registry + tokens + live SFU | **LIVE ✅** |
| **G-FED** Cross-host Spaces | `test_fed_sfu_get_policy.py` + opus@.41→.158 mint + `/sfu/candidates` via live relay | assertion + authd + trust + keystore + nonce + registry + **discovery client** | **LIVE ✅** (mint + discovery, F-8) |
| **G-GLOSSA** mesh over real SFU | `test_glossa_mesh_integration.py` + `scripts/preflight_glossa_live.py` | gatekeeper + session daemon + mesh bus + node + audit gloss | **LIVE ✅** (8/8 over real SFU, F-9) |
| **G-ADAPTER** inbound bridge → chat | `test_adapter_hub.py`, `test_adapter_bind*.py` + Telegram bridge live | AdapterHub + AdapterRegistry + FQID resolve + reply-routing/`/bind` + advocacy | CI-int → **LIVE ✅** (Telegram, F-7) |
| **G-VOICE** voice loop | `voice_engine/voice_session.py` + live STT→LLM→TTS run | Whisper STT + Conversation VO + VoiceEngine + Piper TTS | CI-int → **LIVE ✅** (F-7) |
| **G-GLOSSA-AUDIO** mesh over audio | `test_glossa_audio_mesh.py` | unchanged node + modem + MAC + audio bus | CI-int |
| **G-BLE** multi-hop proximity | `test_ble_mesh.py` | protocol + relay + noise + identity + radio | CI-int → **GATED** (real radio) |
| **G-XPORT** cross-transport | `test_integration.py` (skcomms), `test_smoke.py` | envelope + registry + multiple transports | CI-int |

---

## 3. Real use cases (what a human/agent actually does)

Each use case is the unit of "done." A use case is **done** only at **LIVE ✅**.

| # | Use case | Actor | Backing components | Status |
|---|---|---|---|---|
| **U1** | Agent A sends a chat message to Agent B; B receives it | Lumina↔Opus | G-MSG, transport, history | **LIVE ✅** (inbound receive observed: Claude ping landed in lumina inbox; send path healthy 0 pending/0 dead) — see F-1 |
| **U2** | `@opus` mention auto-generates a reply in-thread | Chef→Opus | advocacy, daemon | **LIVE ⏳** (advocacy engine running; not exercised this pass) |
| **U3** | Create a group, add members, send to group | any | group, guest | **LIVE ⏳** |
| **U4** | QR-pair two devices, then chat (TOFU sig-gate) | Chef + phone | pairing, pairing_gate | **LIVE ⏳** (web pair page live; on-device pair untested) |
| **U5** | Place a 1:1 audio/video call between two browsers | Chef↔Lumina | G-CALL, connectivity | **LIVE ⏳** (runbook exists; re-verify) |
| **U6** | Host an audio Space; speakers + listeners join; raise-hand | Lumina host | G-SPACE, moderation | **LIVE ✅** (2 phones, Town Hall) |
| **U7** | Record a Space with per-speaker consent | host | recorder, consent ledger | **LIVE ⏳** (egress wired; not recorded live) |
| **U8** | An agent on **another machine** joins a Space (federated) | opus@.41 → .158 | G-FED | **LIVE ✅** (2026-06-17 — token mint opus@.41→.158 SFU **+** `/sfu/candidates` returns .158 via the live Nostr relay; F-8); real two-browser *visual* join **LIVE ⏳** |
| **U9** | Two agents negotiate SKGlossa and mesh densely, humans read the gloss | Lumina↔Opus | G-GLOSSA | **LIVE ✅** (2026-06-17 — `preflight_glossa_live.py` 8/8 over the REAL SFU; signed frames, forgery rejection, peer-leave un-cap; F-9) |
| **U10** | Send a file / attachment in chat | any | files, attachments | **LIVE ⏳** |
| **U11** | Voice message: speak → STT → send; receive → TTS | Chef↔Lumina | voice backends, G-VOICE | **LIVE ✅** (2026-06-17 — full loop: real Whisper :18794 → qwen3.5/qwen3.6 → fast Piper CPU TTS, 3.4s vs F5-TTS 113s; F-7) |
| **U12** | Chat over Bluetooth proximity (no internet) after QR-pair | two phones | G-BLE, ble pairing bundle | **GATED** (real BT radio) |
| **U13** | Send a text over LoRa off-grid | node↔node | LoRa transport | **GATED** (LoRa board) |
| **U14** | Bridge: a message from Telegram appears in skchat and vice-versa | Chef via TG | channel adapters, AdapterHub, reply-routing/`/bind`, G-ADAPTER | **LIVE ✅** (2026-06-17 — `@seaBird_Opus_bot` persistent (`skchat-telegram-opus.service`) = real Opus on qwen3.6-27b-abliterated @ 32k ctx, uncensored HTML replies, per-chat memory, 1024-tok; inbound→advocacy→outbound `adapter.send` round-trip; F-7) |
| **U15** | Collaborative lane: chat/whiteboard/screen/watch/doc/term in a session | two users | livekit.html lanes + LaneStore/routes + **app LaneService** | **server persistence: LIVE ✅** (Tier 2 — all 5 lanes persist+replay live on `:8765`, snapshot latest-wins for whiteboard, unknown-lane→400; harness F-3); **web client** mirrors+catches-up; **app data-lane substrate (LaneService) + in-Space chat lane shipped** (Tier 4, `b1763c3`); rich app lanes (whiteboard/screen/watch/doc/term) + live two-browser/phone *visual* collab: **LIVE ⏳** |
| **U16** | Drive an agent swarm from a phone (skharness session-switcher) | Chef + phone | skharness P0 + Flutter | **GATED** (P1 TmuxSpawner + Flutter) |

---

## 4. Coverage gaps / add-on backlog

Tests/use cases that do **not** exist yet — added here so the matrix grows honestly
rather than implying coverage it lacks. Each maps to a Tier in the integration program.

- **Server-side lane dispatcher + persistence** (Tier 2) — U15 has *no* server test;
  lanes live only in `livekit.html` JS. Need: dispatcher unit tests, persistence
  round-trip tests, replay-on-join test.
- ~~**Channel-adapter runtime** (Tier 3)~~ — **RESOLVED 2026-06-17.** `AdapterRegistry`
  instantiated in the skcomms daemon lifespan (Wave 3a keystone) + `GET /adapters` (Wave 2);
  AdapterHub inbound→chat (Wave 4) + reply-routing/`/bind`/`FqidBindingStore` outbound
  (Wave 5). U14 now **LIVE ✅** via `@seaBird_Opus_bot` (F-7). Live Discord/Slack/Matrix
  still **GATED** (bot tokens).
- **Flutter app feature parity** (Tier 4) — no widget/integration tests for
  screen-share/whiteboard/watch/docs/term; `sendData`/`dataChannel` primitive has zero
  callers; coord board screen unrouted. U15/U16 app legs uncovered.
- **Live legs of CI-int groups** (Tier 5) — remaining **LIVE ⏳**: G-CALL, U3/U4/U5/U7/U10.
  (G-GLOSSA, G-VOICE, U8/U9/U11/U14 promoted to LIVE ✅ on 2026-06-17 — F-7/F-8/F-9.)
- ~~**Federation discovery client** (1f gap)~~ — **RESOLVED 2026-06-17.**
  `FederationDiscoveryClient` + `GET /sfu/candidates` (Wave 4) over the live
  `skchat-nostr-relay :7447`; returns the .158 host (F-8). U8's real two-browser
  *visual* join remains **LIVE ⏳**.
- **Real two-browser *visual* federated join** (U8) — token mint + discovery are LIVE;
  the actual cross-host two-browser audio/visual join is still **LIVE ⏳**.
- **Image / vision** — **dropped** from the qwen3.6-27b-abliterated backend
  (`skai-beellama.service`) to free VRAM for 32k ctx (vision `mmproj` removed, `.bak`
  saved). The Telegram bridge is text-only; no vision use case is in scope.

---

## 5. Live findings log (real QA-pass results)

Honest results from actually running things on the live .158 stack — including the
near-misses, because those are the difference between "wired" and "works."

- **F-1 (2026-06-13, U1 messaging):** Daemon up 3.5 days, `transport_ok: true`.
  Sent a real signed `opus→lumina` message → recorded in searchable history, outbox
  **0 pending / 0 dead** (1053 files are the delivered *archive*, not a backlog —
  initially misread as a backlog, then disproved). The message did **not** surface in
  the co-located lumina inbox within ~30s. Root cause is **transport selection, not a
  delivery failure**: the send chose `syncthing`, which syncs to remote *devices*;
  for two identities on the *same* Syncthing instance there is no peer to sync to, so
  a co-located recipient never gets an inbound event. Genuine inbound delivery **does**
  work — a separate `claude→lumina` ping was received live in the same window.
  **Action:** for same-box agent pairs, prefer a local/file transport over syncthing;
  re-verify U1 with the real cross-machine case (.41 ↔ .158), which is the path that
  matters. Filed as a Tier-5 follow-up.

- **F-2 (2026-06-13, Tier 2 lanes — LIVE ✅):** After landing LaneStore/Dispatcher/routes
  and restarting `skchat-webui@{lumina,opus}`, smoke-tested the **live** endpoint on
  `:8765`: `POST /spaces/qa-smoke/lanes/event {"lane":"chat",...}` → `{"ok":true}`;
  `GET /spaces/qa-smoke/lanes/chat/state` → replayed the persisted event; unknown lane
  → `400`. The lane persistence substrate works end-to-end against the running service,
  not just in CI. Remaining for U15: the two-browser *visual* collab (whiteboard strokes
  surviving a refresh) — LIVE ⏳.

- **F-3 (2026-06-14, Tier-5 live run — automated harness):** Authored a reusable
  live verification harness (`scripts/tier5_verify.py`, stdlib-only urllib, no app
  imports) and ran it against the running `skchat-webui@lumina` on `:8765`. Throwaway
  space_id `tier5-<ts>`. **10/10 checks PASS, exit 0:**

  | Check | Result | Detail |
  |---|---|---|
  | health endpoint | **PASS** | HTTP 200, `status=ok service=skchat-webui` |
  | spaces directory (`GET /spaces`) | **PASS** | HTTP 200, JSON `{spaces:[…]}` (1 live: Town Hall) |
  | spaces live page (`GET /spaces/live`) | **PASS** | HTTP 200 (HTML) |
  | lane `chat` persist+replay (log) | **PASS** | POST 200; GET state latest == posted marker |
  | lane `watch` persist+replay (log) | **PASS** | POST 200; GET state latest == posted marker |
  | lane `doc` persist+replay (log) | **PASS** | POST 200; GET state latest == posted marker |
  | lane `term` persist+replay (log) | **PASS** | POST 200; GET state latest == posted marker |
  | lane `whiteboard` persist+replay (snapshot) | **PASS** | 2 snapshots posted; state returns 1 (latest-wins) |
  | unknown-lane POST rejected | **PASS** | HTTP 400 `{"error":"unknown or missing lane 'bogus'"}` |
  | unknown-lane GET rejected | **PASS** | HTTP 400 `{"error":"unknown lane 'bogus'"}` |

  This extends F-2 (which smoke-tested only the `chat` lane) to **all five lanes**
  (chat/watch/doc/term log-append + whiteboard snapshot latest-wins) plus both
  unknown-lane rejection paths, run live against the service. **Recording write-up
  CI** (`tests/test_recording_writeup.py`) also re-run green: **5/5 passed** (the
  transcript→write-up→chat-lane orchestrator + graceful no-transcript path; seams
  faked so no real Whisper/LLM/network). The recording *pipeline-to-live-Space*
  (real OGG egress → Whisper → LLM → posted in a live Space) remains **LIVE ⏳**:
  this pass verifies the unit seams + the lane endpoint the poster targets, not an
  end-to-end recorded Space. Harness is rerunnable: `scripts/tier5_verify.py`.

## 6. Change log

- **2026-06-17** — **Sprint cycle waves 2–6** (skchat `f4acfa1`→`bdb83ce`, skcomms `8ecffe8`;
  all pushed). Baseline grew well past 1082 → **~1829 tests / 106 files** (skcomms 595).
  - *Wave 2 (hardening):* importorskip guards (10 glossa modules), transport
    `_write_local_loopback` + concurrent-race tests, voice `_process_speech` E2E, `GET /adapters`.
  - *Wave 3a (keystone):* single-owner reconnect (killed the daemon/watchdog double-reconnect
    race), short-name `is_loopback()` (**closes F-1**), `AdapterRegistry` instantiated in the
    skcomms daemon lifespan (**U14 keystone**).
  - *Wave 3b (hygiene):* ~25 bare-excepts logged, ruff 69→0, CI format-check, coverage gate
    (fail_under 54, measured 57).
  - *Wave 4 (foundations):* **AdapterHub** (40 tests), **FederationDiscoveryClient** +
    `GET /sfu/candidates` (13), **GlossaMeshGatekeeper** (14), `LiveKitBus.on_leave` un-cap,
    `Conversation` VO into `VoiceEngine.respond`.
  - *Wave 5 (next-phase):* **U14 reply-routing** + `/bind` CapAuth + restart-durable
    `FqidBindingStore` (82), **GlossaMeshSessionDaemon** (10), worship handlers read
    `ctx['convo']` + **VoiceSession**.
  - *Wave 6:* U8 browser federated-join wiring (`static/livekit.html`) + 4 preflight harnesses.
  - **Promoted to LIVE ✅:** **U11** voice (full loop, fast Piper, F-7), **U14** Telegram bridge
    (`@seaBird_Opus_bot` = real Opus, F-7), **U8** token-mint + discovery (F-8), **U9** glossa mesh
    over the real SFU (F-9). New persistent .158 services: `skchat-telegram-opus`,
    `skchat-piper-tts :18797`, `skchat-nostr-relay :7447`. F-7/F-8/F-9/F-10 recorded.
    **Still GATED/⏳:** real two-browser *visual* federated join (U8 leg), live Discord/Slack/Matrix
    bridges (tokens); **image/vision dropped** from qwen3.6 to buy 32k ctx.


- **2026-06-13** — Matrix created. skcomms consolidated to `integration/skcomms-unified`
  (glossa+BLE+LoRa, 497 passing). Baseline captured: skchat 1082, skcomms 497.
  LIVE ✅ to date: U1 (receive leg), U6, U8 (token-mint leg). Everything else CI or
  LIVE ⏳/GATED. First live finding F-1 recorded (co-located syncthing = remote-only).
- **2026-06-14** — Tier 4 (Flutter) progress: coord board routed + surfaced in Profile (`53ab100`); **data-lane substrate `LaneService`** (publish→data-channel+server-mirror, inbound stream, catch-up) + **in-Space text-chat lane** as first consumer (`b1763c3`). Remaining Tier 4: rich lane UIs (whiteboard/screen/watch/doc/term), Spaces/coord→bottom-nav, identity-card real data. skcomms failure-mode fixed (home→~/.skcapstone/skcomms + skcomm: config compat, 0.1.5).
- **2026-06-14** — **Tier-5 live harness** added (`scripts/tier5_verify.py`) + first
  automated live run: **10/10 PASS** against `:8765` (health, spaces dir + live page,
  all 5 lanes persist+replay, snapshot latest-wins, unknown-lane→400). Recording
  write-up CI re-run **5/5**. Flipped **U15 server-persistence** and the **Tier-2 lane**
  row to **LIVE ✅** (all five lanes, not just chat). F-3 recorded. Still LIVE ⏳:
  two-browser *visual* collab + recording-pipeline over a real recorded Space.
- **2026-06-14** — Tier 4 lanes COMPLETE (app): all five collaborative lanes shipped on the LaneService substrate via a lane-chooser FAB in the Space — **chat** + **watch-together** (`2300e96`) + **whiteboard** + **docs** + **screen-share** (`2659fea`, built via swarm). Whiteboard=CustomPaint over snapshot lane; docs=shared last-write text; screen-share=LiveKit screen track + VideoTrackRenderer. Remaining Tier 4: terminal lane (gated on skreachd), Spaces/coord→bottom-nav, identity-card real data; Tier 5 = live two-client verification.

- **F-4 (2026-06-14, skreachd + recording write-up — LIVE ✅):** Enabled both on the .158 webui (env in `webui-{lumina,opus}.env`: `SKREACHD_ENABLED=1` + `SKREACHD_OPERATORS`, `SKCHAT_SPACES_AUTO_WRITEUP=1`).
  - **skreachd terminal exec** verified live via `POST /spaces/{id}/lanes/term/run`: operator `whoami`→`cbrd21` exit 0; `echo`→real output; **non-allowlisted (`rm`)→denied**, **unauthorized identity→denied**, **shell-injection (`ls; rm -rf /`)→denied** (argv-only). All safety gates hold.
  - **Recording→Whisper→LLM→write-up** verified live: full chain on a 75s speech clip → whisper transcript → LLM (`:18783`, claude-haiku-4-5) structured write-up (Summary/Key Points/Action Items) → **posted to the Space chat lane**. Silent recordings post the honest "no transcript" note (graceful). Default whisper model `base`; auto-fires on `/record/stop`.

- **F-5 (2026-06-14, TWO-AGENT cross-party lane round-trip — LIVE ✅):** Two independent
  agents ran concurrently against the SAME Space (`twoagent-qa1`) on `:8765`. **Agent A**
  posted chat + whiteboard(snapshot) + watch(load) (all HTTP 200) and saw **Agent B's** chat
  reply; **Agent B** posted its reply and saw **Agent A on all 3 lanes** — chat & watch as
  appended logs (both events present, ordered), whiteboard as a single latest-wins snapshot.
  Real two-party communication through the live lane store verified (not self-echo). The
  remaining un-verified leg is the real-time LiveKit **data-channel** peer path (needs actual
  browser/SDK clients, not HTTP) — the persistence/catch-up/cross-party substrate is LIVE.

- **F-6 (2026-06-14, real-time LiveKit DATA-CHANNEL lane sync — LIVE ✅):** Closes the one
  leg F-5 left open. Headless two-browser harness `scripts/qa_two_browser.py` (Playwright +
  full Chromium with `--use-fake-device-for-media-stream`) launched **two independent browser
  contexts**, each joined to the SAME live Space (`space-zvteyh73i6b6czb6`, "Town Hall") with
  its **own Space-minted token** (`POST /spaces/{id}/join` → `{url, token, room}`; alice 387B,
  bob 381B). **Both contexts connected to the live SFU** (`wss://noroc2027.tail204f0c.ts.net:8443`,
  real `roomID RM_…`) and saw each other (2 participants). Context A called
  `publishLane({lane:'chat', …})` over the **WebRTC data channel**; context B received it via
  `RoomEvent.DataReceived → routeDataMessage → onChatReceived` and rendered it in `#chat-messages`
  within the timeout. **Real-time peer delivery verified** (distinct from the HTTP lane store of
  F-2/F-5) — PASS on two consecutive runs, exit 0.
  - **Bug fixed in the process:** `static/livekit.html` imported `TrackPublishOptions` (+ unused
    `LocalTrack`) from the livekit-client ESM, which does NOT export it — a **fatal ESM module
    load error** that left `room`/`publishLane`/`connect` undefined, i.e. the call/lane page was
    broken for everyone. Removed both unused imports; page now loads and connects. Also added a
    tiny test hook (`window.__skRoom` / `window.__skPublishLane`, set after `room.connect`) so
    module-scoped state is reachable from `page.evaluate` (no UI behaviour change).
  - **NB on the page used:** the Space page (`/space/{id}`, `space.html`) is audio-only and has
    NO data-channel lane JS. The lane logic lives in `livekit.html`, which joins the SAME room
    (room name == space_id), so this exercises the exact lane wiring against the Space's real room.
  - **Env requirement (cannot run in GitHub CI):** needs the live webui + an SFU reachable from
    the runner with a trusted TLS cert + a full Chromium build. Run locally:
    `~/.skenv/bin/python scripts/qa_two_browser.py`.

- **F-7 (2026-06-17, U11 voice + U14 Telegram bridge — LIVE ✅):** Two epic use cases
  taken live on .158, both on CPU except the qwen3.6 backend.
  - **U11 voice loop:** real **STT (Whisper `:18794`) → LLM (qwen3.5 / qwen3.6) → TTS**,
    full round-trip via `voice_engine/voice_session.py` (`Conversation` VO threaded into
    `VoiceEngine.respond`). TTS cut over to **fast Piper CPU** (`skchat-piper-tts.service`,
    OpenAI `/v1/audio/speech` `:18797`; voice's `SKVOICE_TTS_URL` → here): **3.4 s** vs
    F5-TTS **113 s**. The speak→STT→reply→TTS loop completes live.
  - **U14 Telegram bridge:** **`@seaBird_Opus_bot`** is live + persistent
    (`skchat-telegram-opus.service`, `scripts/telegram_bridge.py`) = **real Opus** — the
    `opus-unhinged` soul built via `SystemPromptBuilder` on **qwen3.6-27b-abliterated @ 32k
    ctx**, uncensored, **HTML-formatted** replies, **per-chat memory**, context-fit to budget,
    1024-token replies. Inbound TG → AdapterHub → advocacy → outbound `adapter.send` reply
    round-trips against the real bot. (Backend: `skai-beellama.service` on .100 5060 Ti
    retuned 8192→32768 ctx by dropping the vision `mmproj` — freed 889 MB, 925 MB headroom,
    ~2.4 s gen; vision traded for context, `.bak` saved.)

- **F-8 (2026-06-17, U8 federation — token mint + discovery, LIVE ✅):** Cross-host
  federated join taken live end-to-end on real infra.
  - **Token mint:** `opus@.41` minted a real speaker token for a Space on the **.158 SFU**
    (`wss://noroc2027.tail204f0c.ts.net:8443`) — verified (re-confirms the earlier mint leg,
    now with per-agent SKAGENT wire identity so opus no longer transmits as lumina).
  - **Discovery client:** `GET /sfu/candidates` returns the **.158 host** via the **live Nostr
    discovery relay** (`skchat-nostr-relay.service :7447`, `scripts/nostr_relay.py`;
    `SKCHAT_NOSTR_RELAYS` → here), backed by `FederationDiscoveryClient` (Wave 4). This closes
    the prior 1f "no discovery client" gap. **Still LIVE ⏳:** the real two-browser *visual*
    cross-host join (token+discovery proven; the actual A/V join is unverified).

- **F-9 (2026-06-17, U9 glossa mesh over REAL SFU — LIVE ✅):** `scripts/preflight_glossa_live.py`
  **PASS 8/8** over the **real SFU** (not FakeBus): two `GlossaMeshSessionDaemon` instances
  exchanged **gatekeeper-signed** frames over a **live data channel**, with **forgery rejection
  ×3** (tampered/wrong-key frames refused pre-decode) and **live peer-leave un-cap**. This is the
  first time the glossa mesh ran end-to-end over real infra (previously CI-only, zero imports
  outside `glossa_mesh/`). The always-decodable audit-gloss invariant holds on the live path.

- **F-10 (2026-06-17, preflight harness suite — local full-path proofs):** Wave 6 added four
  reusable preflight harnesses — `scripts/preflight_{adapter,federation,glossa,voice}.py` — that
  exercise the **full code path locally**, faking **only at the external edge** (real adapter
  registry/hub, real federation mint/verify, real glossa sign/verify, real voice engine; fakes
  only the actual TG network / remote host / SFU socket / mic+speaker). **All exit 0.** These are
  the rerunnable bridge between CI and the LIVE ✅ runs above (the `*_live` variant of glossa,
  `preflight_glossa_live.py`, is the one that hits the real SFU in F-9). Plus Wave 2 hardening:
  transport `_write_local_loopback` + concurrent-race tests, voice `_process_speech` E2E,
  importorskip guards on 10 glossa modules; Wave 3a `is_loopback()` short-name fix **closes F-1's
  same-box transport-selection follow-up** (co-located pairs now resolve to the local loopback
  transport, not syncthing).
