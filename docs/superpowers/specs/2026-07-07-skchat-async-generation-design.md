# SKChat Async Generation — Design Spec

**Date:** 2026-07-07
**Status:** Approved for planning
**Package:** `skchat` (`src/skchat/daemon.py`, `src/skchat/group_responder.py`)

## Problem

The daemon's receive loop (`ChatDaemon.start()`, `daemon.py:376-636`) is
single-threaded. For each polled message it runs the full
`respond()`/`respond_direct()` → send → store chain **inline**, and
`group_responder.generate()` is a blocking `httpx.post(..., timeout=120)`
(`group_responder.py:121`, ~10s typical). While one reply generates, the loop
cannot call `poll_inbox()`, so a second message that arrives during generation
is not received until the current one finishes. Observed symptom: back-to-back
messages to the same agent (<~20s apart) get missed or badly delayed.

## Goal

The poll loop must never block on LLM generation. Every message an agent is
addressed by is received promptly and answered — in order — even under
back-to-back sends.

## Approach — single background worker + queue

Decision: **one dedicated worker thread draining a FIFO queue**, not a thread
pool. Rationale (decided 2026-07-07):

- Cross-agent parallelism already exists — each agent (lumina, opus) is a
  separate daemon process, so `@all` already generates in parallel across
  agents. A pool would only parallelize *one agent's own* backlog.
- Serialized, ordered replies from a single agent are the correct
  conversational behavior; interleaved out-of-order replies read as broken.
- The backend is a shared serial resource (skgateway → ornith on one GPU box),
  so intra-agent parallel workers contend at the backend for ~zero real
  throughput while adding first-ever races on shared state.
- The problem is *missed messages*, not throughput (YAGNI). A bounded pool with
  per-target ordering is the documented future step if throughput pressure
  appears.

## Architecture

```
poll loop thread                         worker thread
────────────────                         ─────────────
poll_inbox()                             q.get()  (blocks when idle)
  for msg in messages:                   _process_message(msg):
    if _route_file_message(msg): continue    group?  respond()  → fan-out (send, locked)
    q.put(msg)          ───────────►         dm?     respond_direct() → send (locked)
  ...periodic outbox/presence/mb...          advocacy / plugin fallbacks
  sleep(interval)                            q.task_done()
```

### Components

1. **`self._genqueue: queue.Queue`** — unbounded FIFO. Unbounded is deliberate:
   a bounded `put()` would block the poll loop when full, reintroducing the
   very stall we are removing. Chat volume is low; if this ever needs a bound,
   it must be `put_nowait` + explicit drop-with-`log()`, never a blocking put.

2. **`self._genworker: threading.Thread`** (daemon thread, name
   `"skchat-genworker"`) started during subsystem init (alongside the existing
   `_init_subsystems_bg`, `daemon.py:364`). Loop:
   ```python
   while self.running or not self._genqueue.empty():
       try:
           msg = self._genqueue.get(timeout=1.0)
       except queue.Empty:
           continue
       try:
           self._process_message(msg)
       except Exception as exc:
           logger.warning("genworker: processing failed: %s", exc)
       finally:
           self._genqueue.task_done()
   ```

3. **`self._process_message(msg)`** — the current inline dispatch block
   (`daemon.py:421-532`: group path + DM path + advocacy fallback + plugin
   fallback) extracted verbatim into a method on `ChatDaemon`. It already reads
   only `self.*` state, so no new parameters. The poll loop's per-message body
   becomes: file-route (keep inline, not LLM-blocking) else `self._genqueue.put(msg)`.

4. **`self._send_lock: threading.Lock`** — guards `transport.send_message(...)`
   calls, which now happen from the worker (fan-out + DM send) concurrently with
   the poll thread's periodic outbox flush. `skcomms` send is not audited for
   concurrent use, so serialize it. skmemory SQLite (`check_same_thread=False`
   + WAL + `busy_timeout`) and append-only `ChatHistory.save` are concurrency-
   tolerant and are left unlocked (documented, not guarded — avoid over-locking).

### Data flow

`poll_inbox()` (archives at receive time — no dup-delivery risk) → `total_received += 1`
(poll thread, unchanged) → `q.put(msg)` → **[worker]** `_process_message` →
`respond()`/`respond_direct()` → `with self._send_lock: transport.send_message(...)`
/ `local_deliver_to_agent(...)` → `store_turn`. `self.advocacy_responses += 1`
moves into the worker — the single worker is the only writer, so no counter race.

### Error handling

- Worker catches per-message exceptions (one failed generation never kills the
  worker) — mirrors the current per-message try/except.
- On `generate()` returning `None` (backend down/timeout), the worker logs and
  drops the reply for that message (current behavior), then continues.

### Shutdown / drain

- `stop()` sets `self.running = False`; the worker loop condition
  (`self.running or not q.empty()`) lets it finish queued work, then the
  `get(timeout=1.0)` unblocks and it exits. `stop()` `join()`s the worker with a
  timeout (e.g. 10s) so in-flight generation isn't hard-killed.
- Add `drain(timeout=None)` → `self._genqueue.join()` for deterministic tests.

## Testing

- **Preserve sync contracts:** `GroupResponder.respond()` / `respond_direct()`
  stay synchronous (the worker calls them), so `test_group_responder.py` and
  `test_daemon_group.py` (direct-call unit tests) stay green untouched.
- **`total_received`** still increments at receive time in the poll loop, so
  `test_daemon.py`'s post-`start()` count assertions stay valid.
- **New unit tests** (`test_daemon.py` / `test_daemon_async.py`):
  - poll loop enqueues rather than generating inline (mock the worker / assert
    `_genqueue` size grows without a `generate()` call on the poll thread);
  - worker drains a queued message → `_process_message` runs → reply sent
    (assert via mocked transport after `daemon.drain()`);
  - back-to-back: enqueue 3 messages while a slow mock `generate` runs → all 3
    received (poll not blocked) and all 3 eventually answered in order;
  - shutdown drains in-flight work and joins the worker within timeout.
- **Integration tests** (`test_daemon_integration.py`) that assert
  `advocacy_responses` must call `daemon.drain()` (or `_genqueue.join()`) before
  asserting, since generation is now off the poll thread. Update those call
  sites; receive-count assertions are unchanged.

## Out of scope

- Thread-pool / parallel intra-agent generation and any per-target ordering
  primitive (only needed with a pool).
- Backend-side concurrency (skgateway/ornith) — unchanged; still serial.
- The model-routing change (`reg:ornith` → `sk-default`) — separate spec
  (router-audit), sequenced after this.
- Async I/O (`asyncio`) migration of the daemon — a threaded worker is the
  minimal change; a full asyncio rewrite is not warranted.

## Files

- Modify: `src/skchat/daemon.py` — add `_genqueue`/`_genworker`/`_send_lock`,
  extract `_process_message`, enqueue in the poll loop, drain/join in `stop()`.
- Modify (send-lock only): the `transport.send_message` call sites in the
  extracted dispatch + the poll-loop outbox flush.
- Test: `tests/test_daemon.py`, new `tests/test_daemon_async.py`, update
  `tests/test_daemon_integration.py` drain calls.
