# Live Channel-Adapter Bridge Test (U14)

**Use case U14** — "Bridge: a message from Telegram appears in skchat and (eventually)
vice-versa." This runbook is the **GATED → live** verification for the channel-adapter
lane: it needs Chef's real Telegram creds + group membership, which CI cannot provide.

> **Honesty rule (this repo):** CI-green ≠ done. This runbook flips the *inbound* leg
> of U14 from `LIVE ⏳` to `LIVE ✅` only when observed end-to-end. The *send-leg*
> (reply routing back to the platform) and the `/bind` CapAuth round-trip are **NOT
> built yet** — they are wave-5 (U14 Phase 2 / Phase 3). Those sections below are
> written as "what the live run will look like once the code lands," and are explicitly
> marked **GATED ON WAVE-5 CODE**. Do not report them as passing.

---

## Purpose

Prove the real inbound pipeline, surface by surface:

```
Telegram event
  → skcomms TelegramAdapter.inbound()           (Telethon user session; normalize)
  → AdapterRegistry._dispatch()                  (resolve_fqid → trust → handler)
  → skchat AdapterHub.handle_inbound()           (ChannelMessage → ChatMessage)
  → ChatHistory.save()                           (unified-memory write)
  → AdvocacyEngine.process_message()             (@mention → skcapstone reply)
  → [reply string]                               ← STOPS HERE today (no send-leg)
```

What this run actually verifies **today**:

1. A real Telegram message lands as a normalized `ChannelMessage` (skcomms).
2. The `AdapterRegistry` dispatches it with a resolved-or-guest FQID + trust level.
3. The skchat `AdapterHub` converts it to a `ChatMessage`, persists it to
   `ChatHistory`, and — when it contains `@opus`/`@claude`/`@ai`/`@lumina` — produces
   an advocacy reply **string** (the reply is captured in `InboundResult.reply`, not
   yet sent back to Telegram).

What it does **not** verify today (wave-5): the reply landing back on the platform,
and the `/bind chef@skworld.io` CapAuth challenge.

---

## Code paths exercised (verified to exist)

| Step | Module / symbol | File |
|---|---|---|
| Normalize TG event | `skcomms.adapters.telegram.TelegramAdapter._normalize_telethon` / `.inbound` | `skcomms/src/skcomms/adapters/telegram.py` |
| Build registry from config | `skcomms.adapters.factory.build_registry_from_config` | `skcomms/src/skcomms/adapters/factory.py` |
| Live registry start (server) | `skcomms.api` lifespan → `build_registry_from_config(load_adapters_block())` → `registry.start()` | `skcomms/src/skcomms/api.py` (≈L131–147) |
| Read `adapters:` block | `skcomms.config.load_adapters_block` | `skcomms/src/skcomms/config.py` (L147) |
| Dispatch inbound | `skcomms.adapters.registry.AdapterRegistry._dispatch` / `._run_inbound` | `skcomms/src/skcomms/adapters/registry.py` |
| FQID resolve / guest mint | `TelegramAdapter.resolve_fqid`; registry guest-FQID `{channel}_guest_{platform_id}@ext` | telegram.py L756 / registry.py L142–144 |
| skchat landing zone | `skchat.adapter_hub.AdapterHub.handle_inbound` → `InboundResult` | `skchat/src/skchat/adapter_hub.py` |
| Persist | `skchat.history.ChatHistory.save` | `skchat/src/skchat/history.py` (L88) |
| Auto-reply | `skchat.advocacy.AdvocacyEngine.process_message` / `should_advocate` | `skchat/src/skchat/advocacy.py` |
| Adapter health surface | webui `GET /adapters` → `_get_adapter_registry()` | `skchat/src/skchat/webui.py` (L158, L202) |
| Identity persistence | `TelegramAdapter.bind_fqid` → `telegram-ids.yaml` | telegram.py L760 |

**Smoke tool (read-only, no send):** `skcomms/scripts/telegram_smoke.py`.

---

## ⚠️ Architecture reality you must understand before running

These are not bugs — they are the current shape of the code, and they determine what
"pass" means.

1. **Two advocacy contracts that do not line up.** The skcomms `AdapterRegistry`
   hub-mode dispatch (`registry.py` `_dispatch`) calls
   `hub.advocacy.on_channel_message(msg, sender_fqid=...)`. The skchat
   `AdvocacyEngine` has **no** `on_channel_message` method — it exposes
   `process_message(ChatMessage)`. So the registry's *hub mode* cannot drive skchat's
   advocacy directly. The bridge that does line up is the skchat-side **`AdapterHub`**
   (`adapter_hub.py`), which calls `process_message`. The registry must therefore be
   run in **handler mode** with a small inbound handler that forwards to an
   `AdapterHub` (see Step 4). This forwarding handler is the **one piece of glue this
   live run stands up by hand** — it is not yet committed wiring.

2. **No skchat daemon instantiates the registry.** `AdapterHub` is referenced only by
   `webui.py`'s `/adapters` health endpoint, which reads
   `skchat.integration.adapter_registry` — an attribute that **does not exist** in
   `integration.py` today, so `/adapters` returns `[]` until something sets it. The
   live driver is the **skcomms** side (`skcomms.api` lifespan), not skchat.

3. **Telegram = Telethon user session, not a bot, in the production path.** The
   adapter's first-class path is `api_id` + `api_hash` + a `.session` file (Lumina
   participates as a *user*). `bot_token` is only a fallback. For the DR-Chiro group
   (`-5134021983`) you need the **user account to be a member** — a bot token will not
   read that group's history.

4. **`-5134021983` membership gap is real and documented** (telegram.py module
   docstring). Polling it returns an empty update list / `ChannelPrivateError` until
   the account is added.

5. **Send-leg + `/bind` are unbuilt.** There is `AdapterRegistry.send_to_adapter` and
   `TelegramAdapter.send`, but **nothing wires the advocacy reply back through them**,
   and there is **no `/bind` command handler** anywhere in `src/` (grep-confirmed) —
   only `TelegramAdapter.bind_fqid` persistence exists. Treat Steps 7–8 as GATED.

---

## Prerequisites

### Credentials / accounts (Chef supplies)
- **Telegram API creds** for the Lumina *user* account: `TELEGRAM_API_ID`,
  `TELEGRAM_API_HASH` (from <https://my.telegram.org>).
- **An authorized Telethon session file** at
  `~/.skcapstone/agents/lumina/telegram.session` (one-time interactive auth — see
  Setup). The account must be **a member of the test group**.
- **A reachable test group.** Either:
  - **DR-Chiro `-5134021983`** — *requires adding the Lumina user account to the
    group first* (membership gap above); **or**
  - **A throwaway group** you create and add the Lumina account to (recommended for a
    first run — no production-group risk).
- *(Optional, fallback only)* a **bot token** if you choose Bot-API mode instead of
  Telethon. Bot mode cannot read arbitrary group history; prefer Telethon.

### Services / hardware
- This box (`.158`, `noroc2027`) with `~/.skenv` venv.
- `telethon` installed: `~/.skenv/bin/pip install 'skcomms[telegram]'`.
- skcomms API server runnable on `:9384` (it owns the live registry lifespan).
- skchat daemon / webui for the advocacy + history side (`:9385` health, `:8765`
  webui). The advocacy reply path shells out to skcapstone consciousness
  (`AdvocacyEngine._call_consciousness`), so skcapstone must be importable in
  `~/.skenv`.
- For the (gated) `/bind` leg: a **real capauth HTTPS endpoint** and a **persistent
  adapter identity store** (`telegram-ids.yaml`) — neither is wired to a challenge
  flow yet.

### Config file
- `~/.skcapstone/skcomms/config.yml` exists. Its `adapters:` block is currently
  **empty** (`load_adapters_block` returns `{"adapters": {}}`); Setup adds it.

---

## Setup

> Run everything from `~` (NOT from `smilintux-org/`) to avoid the documented
> `skmemory` namespace collision.

### 1. Install the Telegram extra + verify the session

```bash
~/.skenv/bin/pip install 'skcomms[telegram]'

# One-time interactive auth IF the .session file does not exist / is not authorized.
# Creates ~/.skcapstone/agents/lumina/telegram.session
~/.skenv/bin/python - <<'PY'
from telethon.sync import TelegramClient
import os
api_id  = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
sess = os.path.expanduser("~/.skcapstone/agents/lumina/telegram.session")
with TelegramClient(sess, api_id, api_hash) as c:
    me = c.get_me()
    print("authorized as:", me.first_name, "@%s" % (me.username or "none"), "id=", me.id)
PY
```

**Expected:** prints the Lumina account identity. No traceback.

### 2. Read-only connectivity smoke (no message sent)

```bash
cd /home/cbrd21/clawd/skcapstone-repos/skcomms
TELEGRAM_API_ID=$TELEGRAM_API_ID TELEGRAM_API_HASH=$TELEGRAM_API_HASH \
  ~/.skenv/bin/python scripts/telegram_smoke.py --chat <TEST_CHAT_ID>
```

Use your throwaway group's id, or `-5134021983` once the account is a member.

**Expected:** `Connected as: …`, a dialog list that **includes the test group**, and up
to 5 recent messages printed as
`msg_id=… kind=text sender='…' text='…'` (these went through
`_normalize_telethon`). If the test group is **absent** from the dialog list or
messages error with `ChannelPrivateError`, the account is **not a member** — fix that
before continuing (this is the #4 gap).

### 3. Add the `adapters:` block to the skcomms config

Edit `~/.skcapstone/skcomms/config.yml` and add, under the `skcomms:` section
(`load_adapters_block` honors a `skcomms:` / `skcomm:` wrapper or top-level):

```yaml
skcomms:
  adapters:
    telegram:
      enabled: true
      session_file: "~/.skcapstone/agents/lumina/telegram.session"
      api_id: "${TELEGRAM_API_ID}"
      api_hash: "${TELEGRAM_API_HASH}"
      poll_interval_s: 2
      rooms:
        test_group:
          chat_id: "<TEST_CHAT_ID>"        # e.g. -5134021983 or your throwaway group
          agent_fqid: "lumina@skworld.io"
          allow_untrusted: true
      identity_store: "~/.skcapstone/skcomms/adapters/telegram-ids.yaml"
```

`${TELEGRAM_API_ID}` / `${TELEGRAM_API_HASH}` are expanded by
`factory.expand_env` from the process environment at registry-build time, so export
them in the shell that launches the server (Step 5). The factory **gates** on a
required token field; for Telegram that field is `bot_token`
(`REQUIRED_TOKEN_FIELD["telegram"] == "bot_token"`). **Because we use the Telethon
path (no `bot_token`), `build_registry_from_config` will SKIP this adapter as
"token-gated."** Two ways forward:

- **(A) Recommended for this run:** drive the adapter directly with the standalone
  harness in Step 4 (bypasses the token gate; this is the cleanest, most honest live
  run of the inbound pipeline).
- **(B) Server path:** add a dummy `bot_token: "telethon"` field to satisfy the gate
  so `build_registry_from_config` constructs the adapter; `connect()` still prefers the
  injected/real Telethon client and ignores the dummy token. Use only if you want to
  exercise the `skcomms.api` lifespan wiring (Step 5). Note this is a workaround for
  the factory's bot-centric gate, not a designed feature.

---

## Procedure

### Step 4 — Drive the real inbound pipeline (PRIMARY live test)

This stands up the **handler-mode** registry forwarding to a skchat `AdapterHub`, with
a **real Telethon-backed** `TelegramAdapter`. It is the honest end-to-end run of every
verified code path above.

Save as `/tmp/u14_live_inbound.py` and run with
`cd ~ && ~/.skenv/bin/python /tmp/u14_live_inbound.py`:

```python
import asyncio, os, tempfile
from telethon import TelegramClient

from skcomms.adapters.telegram import TelegramAdapter
from skcomms.adapters.registry import AdapterRegistry
from skchat.adapter_hub import AdapterHub
from skchat.history import ChatHistory
from skchat.advocacy import AdvocacyEngine

CHAT_ID = os.environ["TEST_CHAT_ID"]            # e.g. "-5134021983"
SESS = os.path.expanduser("~/.skcapstone/agents/lumina/telegram.session")

async def main():
    tg_client = TelegramClient(SESS, int(os.environ["TELEGRAM_API_ID"]),
                               os.environ["TELEGRAM_API_HASH"])

    adapter = TelegramAdapter(
        config={
            "session_file": SESS,
            "poll_interval_s": 2,
            "rooms": {"test": {"chat_id": CHAT_ID, "agent_fqid": "lumina@skworld.io"}},
            "identity_store": os.path.expanduser(
                "~/.skcapstone/skcomms/adapters/telegram-ids.yaml"),
        },
        telethon_client=tg_client,     # real, injected — production inbound path
    )

    # skchat landing zone: real history + real advocacy, no resolve_fqid map yet
    # (every sender will be UNTRUSTED + guest-FQID until /bind exists).
    hub = AdapterHub(
        history=ChatHistory(history_dir=tempfile.mkdtemp(prefix="u14-hist-")),
        advocacy=AdvocacyEngine(identity="capauth:lumina@skworld.io"),
        resolve_fqid=None,
        agent_identity="capauth:lumina@skworld.io",
    )

    async def handler(msg, fqid, trust):
        # registry already resolved fqid+trust; let the hub do the canonical
        # conversion + persist + advocacy and report the reply string.
        res = hub.handle_inbound(msg)
        print(f"[inbound] from={res.fqid} trust={res.trust} "
              f"kind={msg.kind.value} text={msg.text[:80]!r}")
        if res.reply:
            print(f"[advocacy reply — NOT yet sent to platform (wave-5)]:\n{res.reply}\n")

    registry = AdapterRegistry(inbound_handler=handler)   # HANDLER mode (see arch #1)
    registry.register(adapter)
    await registry.start()
    print("U14 live inbound running. Post a message in the test group "
          "(include @lumina to trigger advocacy). Ctrl-C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await registry.stop()
        await tg_client.disconnect()

asyncio.run(main())
```

```bash
export TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TEST_CHAT_ID=-5134021983
cd ~ && ~/.skenv/bin/python /tmp/u14_live_inbound.py
```

**Action:** from a phone/desktop Telegram client, post a plain message in the test
group, then a second message containing `@lumina`.

**Expected observation:**
- Within ~`poll_interval_s` (2s) of each post, a `[inbound] from=… trust=untrusted
  kind=text text='…'` line appears. `trust=untrusted` and a
  `telegram_guest_<your_tg_user_id>@ext` FQID are **correct** here — no binding exists
  yet (this is the registry guest-mint path, registry.py L142–144).
- The `@lumina` message additionally prints
  `[advocacy reply — NOT yet sent to platform (wave-5)]:` followed by a generated
  reply (skcapstone consciousness output). The plain message prints **no** reply
  (`should_advocate` returns False).
- **Reply does NOT appear back in the Telegram group** — that is the unbuilt send-leg.

### Step 5 — (Optional) verify the server-lifespan registry wiring

Only if you want to prove the `skcomms.api` lifespan path (arch #2). Requires the
config block from Step 3 with the `bot_token: "telethon"` workaround (3B).

```bash
export TELEGRAM_API_ID=... TELEGRAM_API_HASH=...
cd ~ && ~/.skenv/bin/uvicorn skcomms.api:app --host 127.0.0.1 --port 9384
```

**Expected:** in the server log, a line
`Channel adapters started: 1 built ['telegram'], N skipped [...]`. If you see
`0 built … 1 skipped ['telegram']`, the token gate rejected it — apply 3B.

> Note: the server lifespan builds the registry in **lightweight (no-hub) mode**
> (`build_registry_from_config` passes no hub/handler). Per `_dispatch`, inbound
> messages are therefore **logged and dropped** — there is no advocacy/history on this
> path until the hub or handler is wired. So Step 5 proves *adapter start + connect*,
> not the full inbound→reply loop. Step 4 is the real functional test.

### Step 6 — Confirm the unified-memory write (history persisted)

After Step 4 has seen at least one message, in a second shell:

```bash
cd ~ && ~/.skenv/bin/python - <<'PY'
# Point at the SAME history_dir printed-by/used-in Step 4 if you want to re-open it;
# for a quick check, use the live daemon's ChatHistory instead:
from skchat.history import ChatHistory
h = ChatHistory()
for m in h.get_messages(limit=10):
    md = getattr(m, "metadata", {}) or {}
    print(m.sender, "→", m.recipient, "|", md.get("channel"), md.get("trust"),
          "|", (m.content or "")[:60])
PY
```

**Expected:** the inbound Telegram message is present with
`metadata.channel == "telegram"`, `metadata.trust == "untrusted"`,
`metadata.platform_id == <tg user id>`, and `recipient == capauth:lumina@skworld.io`.
(If Step 4 used a throwaway temp `history_dir`, re-open that path instead of the
default `ChatHistory()`.)

### Step 7 — Reply lands back on the platform — **GATED ON WAVE-5 CODE**

**Do not run / do not mark pass.** There is no code wiring `InboundResult.reply` →
`AdapterRegistry.send_to_adapter` / `TelegramAdapter.send`. When U14 Phase 2 lands, the
expected observation will be: the `@lumina` reply from Step 4 appears as a new message
**in the Telegram group**, authored by the Lumina user account, within a few seconds.
Until then this leg is `LIVE ⏳` and blocked on wave-5.

### Step 8 — `/bind chef@skworld.io` CapAuth round-trip — **GATED ON WAVE-5 CODE**

**Do not run / do not mark pass.** Grep-confirmed: there is **no `/bind` command
handler** in `src/` — only `TelegramAdapter.bind_fqid` (which persists a
`canonical_key → fqid` mapping to `telegram-ids.yaml`). The CapAuth challenge
round-trip (post `/bind chef@skworld.io` → hub issues a challenge → Chef proves
key ownership → hub verifies → `bind_fqid`) is **unbuilt** (U14 Phase 3). It also
needs a **real capauth HTTPS endpoint** (none is wired) and the **persistent identity
store** (the YAML exists as a sink but nothing populates it via a verified flow).

When it lands, the expected observation: after a successful `/bind`, the same sender's
next message in Step 4 shows `trust=verified` and `fqid=chef@skworld.io` (not the
`telegram_guest_…@ext` guest FQID), and `telegram-ids.yaml` gains a
`telegram:user:<id>: chef@skworld.io` entry that **survives a restart** of Step 4.

---

## Pass / Fail criteria

| # | Leg | Pass condition | Status today |
|---|---|---|---|
| 1 | Telethon session | Step 1 prints the Lumina identity, no traceback | live-runnable |
| 2 | Read-only smoke | Step 2 lists the test group + ≥1 normalized message | live-runnable (needs membership) |
| 3 | Real inbound → normalize | Step 4 prints `[inbound] … kind=text` within ~2s of a post | live-runnable |
| 4 | Guest FQID + trust | inbound shows `trust=untrusted` + `telegram_guest_<id>@ext` | live-runnable |
| 5 | Unified-memory write | Step 6 shows the message with `metadata.channel=telegram` | live-runnable |
| 6 | @mention advocacy | `@lumina` post yields a non-empty `res.reply`; plain post yields none | live-runnable (needs skcapstone) |
| 7 | Server lifespan start | Step 5 log: `… 1 built ['telegram']` | live-runnable (needs 3B workaround) |
| 8 | **Reply → platform** | reply appears in the TG group | **FAIL by design — wave-5 unbuilt** |
| 9 | **/bind round-trip** | sender flips to `trust=verified`, persists across restart | **FAIL by design — wave-5 unbuilt** |

**Overall verdict for this pass:** PASS if legs 1–7 are observed. Legs 8–9 are
**expected-blocked** and must be reported as `GATED ON WAVE-5 CODE`, not as failures of
this run.

---

## Current status

| Leg | Verification level |
|---|---|
| Adapter units (TG ~100%, registry, factory, hub) | **CI** — `test_telegram_adapter.py` (43), `test_channel_adapter.py` (46), `test_adapter_hub.py` (40), factory/config tests |
| Registry instantiated + daemon loop | **LIVE ⏳ (Tier 3)** — instantiated in `skcomms.api` lifespan, but never with a hub/handler that reaches skchat advocacy; this runbook's Step 4 supplies the missing handler |
| Real inbound bridge (TG → ChatHistory → advocacy reply string) | **GATED → this run** — needs Chef's creds + group membership; Steps 1–6 |
| Reply routing back to platform (send-leg) | **GATED ON WAVE-5 CODE (U14 Phase 2)** — `send_to_adapter`/`adapter.send` exist; nothing wires the reply through them |
| `/bind` CapAuth challenge round-trip | **GATED ON WAVE-5 CODE (U14 Phase 3)** — no `/bind` handler; only `bind_fqid` persistence; needs real capauth HTTPS endpoint |

**Matrix rows this run can flip:** §1l "Registry instantiated + daemon loop"
(`LIVE ⏳` → `LIVE ✅` for the *handler-mode inbound* path) and the inbound half of
**U14** in §3. Record the result as a new **F-#** entry in §5 of
`docs/qa/skworld-comms-verification-matrix.md`, including which test group was used and
whether membership had to be granted. Leave the U14 send-leg + `/bind` rows at
`LIVE ⏳` / `GATED` until wave-5 code lands.

### Cleanup

```bash
# Stop Step 4 (Ctrl-C) and Step 5 (Ctrl-C) processes.
rm -f /tmp/u14_live_inbound.py
# If you added the dummy bot_token (3B) only for Step 5, remove it from config.yml.
# The Telethon .session and telegram-ids.yaml are persistent identity — keep them.
```
