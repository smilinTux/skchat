# Message Log Authoritative (event-sourced) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline, staged) to implement task-by-task. Steps use checkbox (`- [ ]`).

**Goal:** Make one append-only event log the single source of truth for skchat messages, so every surface (app/webui, MCP/agent, guest) reads identical history, mutations reach the one logical message, and the multi-store write race + `1+N` fan-out amplification are retired.

**Architecture:** `message_log.py` (`MessageLog`, store E) is already the correct shape — SQLite/WAL, `PRIMARY KEY (conversation_id, seq)`, immutable globally-unique `message_id`, `(conversation_id, client_dedup_key)` idempotency index. It is currently flag-gated OFF (`SKCHAT_MESSAGE_LOG`), write-only, populated on only one webui path. This plan PROMOTES it to authoritative: all writers append here first; readers read here; the JSONL day-files (A) and SKMemory `_store` (B) become derived projections; the `last_message` denormalization (C) is computed. Delivery fan-out (transport spool files) is KEPT; only history fan-out is retired.

**Tech Stack:** Python 3.10+, SQLite/WAL, Pydantic `ChatMessage`. Live services share `~/.skchat`: `skchat-daemon`, `skchat-webui@lumina`, `skchat-mcp`, telegram bridges, `jarvis-heartbeat`. Downtime is authorized: stop all writers together during cutover stages.

## Global Constraints

- **Flag-gated rollout, but the flag flips to ON as the default at Stage 2 cutover.** `SKCHAT_MESSAGE_LOG` gates dual-write (Stage 1) then read-from-log (Stage 2+). Keep a documented one-line rollback (flag OFF + restart) at every stage until Stage 5.
- **Roll all writers together.** The log is per-`SKCHAT_HOME`; a mixed fleet (some writing the log, some not) diverges. Stop `skchat-daemon skchat-webui@lumina skchat-mcp skchat-telegram-opus skchat-telegram-lumina jarvis-heartbeat` together for cutover; restart together.
- **Backfill before read-cutover.** Before any reader trusts the log (Stage 2), backfill it from existing JSONL (A) so no history is lost. Idempotent via `client_dedup_key`.
- **Never lose a message.** Every stage keeps the old stores written until the stage that explicitly retires them, and each retirement ships only after read-from-log is proven for that surface.
- **conversation_id canonicalization (pin exactly):** group → `group:<group_id>`; 1:1 DM → `dm:<a>|<b>` where `<a>,<b>` are the two participant URIs sorted lexicographically (so both directions map to one conversation). Put this in ONE helper `message_log.conversation_id_for(message)` and use it everywhere.
- **client_dedup_key:** `sha256(sender + "|" + conversation_id + "|" + content + "|" + iso_ts_seconds)` unless the message already carries a client-supplied idempotency key. One helper, used by every writer.
- **Tests run from `~`** (skmemory namespace collision): `cd ~ && ~/.skenv/bin/python -m pytest <repo>/tests/ -q -m 'not live'`.
- **No em/en dashes** anywhere (code, comments, commits, docs).

---

### Task 1: Log core hardening — canonical ids + idempotent record()

**Files:**
- Modify: `src/skchat/message_log.py` (add `conversation_id_for`, `dedup_key_for`, a `record(message)` convenience that derives both and appends idempotently; confirm WAL + unique-index upsert-or-ignore)
- Test: `tests/test_message_log.py` (extend)

**Interfaces:**
- Produces: `MessageLog.record(msg: ChatMessage) -> int` returns the assigned `seq` (or the existing seq if the `client_dedup_key` already present — idempotent). `conversation_id_for(msg) -> str`, `dedup_key_for(msg) -> str` module-level.

- [ ] Step 1: Write failing tests — `record()` of the same message twice yields the same seq and one row; group vs dm `conversation_id_for` canonicalization; two DM directions collapse to one conversation_id.
- [ ] Step 2: Run, verify fail.
- [ ] Step 3: Implement the helpers + `record()` (INSERT ... ON CONFLICT(conversation_id, client_dedup_key) DO NOTHING, then SELECT the seq).
- [ ] Step 4: Run tests, PASS.
- [ ] Step 5: Commit `feat(message-log): canonical conversation_id + idempotent record()`.

### Task 2: Dual-write — every send/receive path appends to the log

**Files:**
- Modify: `src/skchat/transport.py` (send `:553/584/614/643/654`, receive `poll_inbox :796/805`), `src/skchat/daemon_proxy.py` (`_persist :167`, already has `_shadow_log`), `src/skchat/daemon_proxy_groups.py` (`fan_out_send :938`), `src/skchat/guest_group_routes.py` (`guest_send :553`), `src/skchat/mcp_server.py` (`_handle_group_send :2156`), `src/skchat/group.py` (`send :982`)
- Test: `tests/test_message_log_dualwrite.py` (new)

**Interfaces:**
- Consumes: `MessageLog.record` from Task 1. A single shared `ChatHistory.record_event(msg)` wrapper that appends to the log when `SKCHAT_MESSAGE_LOG` is on (so callers add one line each).

- [ ] Step 1: Failing test — drive each send path (group via fan_out_send, dm via transport, guest via guest_send, MCP group send) with the flag on; assert exactly ONE log row per logical message on the right conversation_id (fan-out member copies must NOT create N log rows — record the canonical event once, keyed by dedup).
- [ ] Step 2: Run, verify fail.
- [ ] Step 3: Implement — add `record_event()` to `ChatHistory`; call it once per logical message at each canonical write site (NOT in the per-member loop). Flag-gated; old A/B writes stay.
- [ ] Step 4: Run tests, PASS. Backfill script `scripts/backfill_message_log.py` reads all JSONL (A), dedups by recipient=="group:*" canonical + id, records into the log; idempotent.
- [ ] Step 5: Commit `feat(message-log): dual-write all send/receive paths + backfill script`.

### Task 3: Read cutover — one log feeds every surface

**Files:**
- Modify: `src/skchat/daemon_proxy_groups.py` (`group_thread_messages :1038` → read log), `src/skchat/daemon_proxy.py` (`_lumina_messages :452`, `_group_messages :417`), `src/skchat/guest_group_routes.py` (`_guest_messages :452`), `src/skchat/mcp_server.py` (`check_inbox`, `skchat_inbox :4318`, `get_group_history :3315`, `get_thread`, `skchat_conversation :4538`), `src/skchat/agent_comm.py` (`get_inbox :430`)
- Test: `tests/test_message_log_read.py` (new)

**Interfaces:**
- Consumes: `MessageLog.read(conversation_id, since_seq=, limit=)` (already exists `message_log.py:204`) → list of events → adapt to `ChatMessage` for existing callers via a `log_event_to_message()` shim.
- Produces: app/webui group view, MCP `get_group_history`, and guest conversation all return the SAME ordered history for a conversation (the store-divergence bug is gone).

- [ ] Step 1: Failing test — send one group message via the MCP path and one via the webui path; assert BOTH the MCP `get_group_history` reader and the webui `group_thread_messages` reader return both messages, identically ordered (today they read B vs A and diverge).
- [ ] Step 2: Run, verify fail.
- [ ] Step 3: Implement readers to read the log (behind the flag; fall back to A/B when off). Keep the recipient-filter dedup path only for the flag-off case.
- [ ] Step 4: Run tests, PASS.
- [ ] Step 5: **CUTOVER** — stop all writers, run backfill, flip `SKCHAT_MESSAGE_LOG=1` fleet-wide (drop-in), restart all. Verify a real cross-surface read (app group view == MCP get_group_history). Commit `feat(message-log): read cutover, unified history across surfaces`.

### Task 4: Retire history fan-out + denormalized last_message

**Files:**
- Modify: `daemon_proxy_groups.py` `fan_out_send` (drop the per-member `hist.save` loop `:947-971`, KEEP transport/local delivery), `guest_group_routes.py` `guest_send`/`guest_file_upload` (drop per-member `hist.save`), `group.py` `send`; drop `last_message`/`last_message_time` writes and compute from the log in `group_to_conversation :566`.
- Test: `tests/test_message_log_fanout_retired.py` (new)

- [ ] Step 1: Failing test — a group send writes ONE history/log event (not `1+N`); the conversation-list `last_message` equals the log's latest event for the group; per-member delivery (transport spool) still happens.
- [ ] Step 2: Run, verify fail.
- [ ] Step 3: Implement — remove the history-copy fan-out (keep delivery fan-out), compute `last_message` from `MessageLog.read(gid)[-1]`.
- [ ] Step 4: Run tests + full suite, PASS.
- [ ] Step 5: Commit `feat(message-log): retire history fan-out copies + derive last_message`.

### Task 5: Mutations as events + demote SKMemory to projection + close lock gaps

**Files:**
- Modify: `history.py` (`set_reaction/clear_reaction/edit_message/record_receipt` append mutation events instead of `update_message` full-file rewrite; readers fold events by `message_id`; make `prune()` take `_write_lock` + atomic write), `mcp_server.py`/`agent_comm.py` (B readers already moved in Task 3; make `store_message` a search-index projection, not a source of truth), `daemon_proxy_groups.py` `save_group` lock note.
- Test: `tests/test_message_log_mutations.py` (new)

**Interfaces:**
- Produces: a reaction/edit/receipt is an appended `MessageLog` event referencing the immutable `message_id`; `read()` folds them so a reaction reaches the ONE logical message (fixes the "reaction hits one fan-out copy" bug §3/§6).

- [ ] Step 1: Failing test — react to a group message; every reader (app, MCP, guest) sees the reaction on the single logical message; an edit folds; concurrent react during a send loses nothing.
- [ ] Step 2: Run, verify fail.
- [ ] Step 3: Implement mutation events + fold-on-read; retire `update_message` for these paths; SKMemory `store_message` becomes a derived search projection rebuilt from the log; fix `prune()` locking.
- [ ] Step 4: Run full suite, PASS.
- [ ] Step 5: Commit `feat(message-log): mutations as events, SKMemory as projection, prune locking`.

### Task 6: Cleanup + docs + soak

- [ ] Step 1: Remove the flag-off legacy branches once soaked (or keep one release as rollback); update `CLAUDE.md` "Module Map" + `docs/`.
- [ ] Step 2: Run the whole suite green; manual live smoke (send/receive/react across app + MCP + guest); confirm one log row per message in `~/.skchat`.
- [ ] Step 3: Commit + memory update.

---

## Self-Review notes
- Spec coverage: all 7 consolidation opportunities from the architecture map map to a task (canonical log T1; dual-write T2; unified read T3; retire fan-out + last_message T4; mutations-as-events + SKMemory-projection + prune/save_group locks T5; three-group-send-impls unified via the shared `record_event` in T2/T4).
- Data safety: A and B stay written until T4/T5 retire them, each after read-from-log is proven; backfill before read-cutover; idempotent dedup key means re-runs are safe.
- In-situ decisions (trace-then-implement, do NOT guess): exact `MessageLog.append` signature + return; whether `read()` returns rows or dataclasses (add `log_event_to_message` shim accordingly); the DM `conversation_id` participant URIs available at each send site.
