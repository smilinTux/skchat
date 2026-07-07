# SKChat Async Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the skchat daemon's poll loop never block on LLM generation, so back-to-back messages to one agent are all received and answered in order.

**Architecture:** Extract the per-message dispatch (currently inline in the poll loop) into a `_process(msg)` closure, then move its execution onto a single dedicated worker thread that drains a FIFO `Queue`. The poll loop only receives + enqueues. `transport.send_message` is serialized with a lock (worker + poll-thread outbox both call it); ordered replies, no thread-pool.

**Tech Stack:** Python 3.10+, `queue.Queue`, `threading.Thread`/`threading.Lock`, pytest. Package `skchat`, editable-installed in `~/.skenv`.

## Global Constraints

- Run all commands and tests from `~/` (NOT the repo dir) to avoid the `skmemory` namespace collision: `cd ~ && ~/.skenv/bin/python -m pytest <path> -q`.
- Line length 99 (ruff E501 ignored); target Python 3.10+.
- Single worker only — NO thread pool, NO parallel intra-agent generation (decided in the spec: ordered replies, no new races on shared `ChatTransport`/counter).
- Name collision: `start()` has a local variable `queue` (the outbox queue, `daemon.py:282`). Do NOT `import queue` and reference bare `queue` inside `start()`. Use `from queue import Queue, Empty` at module top and reference `Queue`/`Empty` only.
- `GroupResponder.respond()` / `respond_direct()` MUST stay synchronous (the worker calls them) — do not make them async.
- `self.total_received` MUST keep incrementing at receive time in the poll loop (unchanged), so existing count assertions in `tests/test_daemon.py` stay valid.
- The daemon is editable-installed; after code changes a live run needs `systemctl --user restart skchat-daemon skchat-daemon-opus` — but this plan only requires pytest, not a live restart.

---

### Task 1: Extract per-message dispatch into a `_process(msg)` closure (pure refactor)

Behavior-preserving. Moves the inline dispatch block (`daemon.py:406-532`) into a closure `_process(msg)` defined in `start()` just before the poll loop, and calls it inline. The two `continue` statements that skip remaining handlers for a message (lines 487, 509) become `return`. No async yet — tests stay green.

**Files:**
- Modify: `src/skchat/daemon.py` — add `_process` closure in `start()` (after the bg-init thread start, ~line 364, before `_start_health_server()` at 369); replace poll-loop body lines 406-532 with a `_process(msg)` call.
- Test: `tests/test_daemon.py`

**Interfaces:**
- Produces: a `start()`-local closure `def _process(msg) -> None` capturing the `start()` locals `group_responder`, `group_cfg`, `identity`, `history`, `engine`, `plugin_registry`, `transport`. Same free variables the inline block reads today.

This task is a **pure, behavior-preserving extraction**. TDD's "write a failing test" does not apply — there is no new behavior. The gate is the existing daemon suites staying green (they drive `start()` through the poll loop). Concrete dispatch-through-loop coverage is added in Task 2's async test, which fully exercises `_process` via the worker.

- [ ] **Step 1: Capture the green baseline before touching code**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon.py tests/test_daemon_group.py -q`
Expected: PASS. Record the passed count — Task 1 must end with the same tests passing.

- [ ] **Step 2: Extract the dispatch into `_process(msg)`**

In `src/skchat/daemon.py`, after `threading.Thread(target=_init_subsystems_bg, ...).start()` (line 364) and before `self._log(f"SKChat daemon starting ...")` (366), insert the closure. It is the verbatim body of current lines 406-532 with `continue`→`return`:

```python
        def _process(msg) -> None:
            """Dispatch one received message (group reply / DM reply / advocacy /
            plugins). Runs the blocking generate→send→store chain. Captures the
            start()-local subsystems; None-checked so it is safe before bg init
            populates them."""
            sender_short = msg.sender.split("@")[0].replace("capauth:", "")
            preview = msg.content[:60] + ("..." if len(msg.content) > 60 else "")
            try:
                import subprocess
                from .notifications import desktop_notifications_enabled
                if desktop_notifications_enabled():
                    subprocess.run(
                        ["notify-send", "SKChat", f"[{sender_short}] {preview}"],
                        capture_output=True,
                    )
            except Exception as exc:
                logger.warning("notify-send failed: %s", exc)
            if group_responder is not None and _is_group_message(msg, group_cfg.groups):
                try:
                    reply = group_responder.respond(msg)
                    if reply:
                        _who = group_cfg.agent.capitalize()
                        if not reply.lstrip().lower().startswith(
                            (_who.lower(), f"**{_who.lower()}")
                        ):
                            reply = f"{_who}: {reply}"
                        from .daemon_proxy_groups import load_group
                        gid = (msg.thread_id or msg.recipient).replace("group:", "")
                        grp = load_group(gid)
                        if grp is not None:
                            grp.send(reply, sender=identity, transport=None, history=history)
                            from .daemon_proxy_groups import local_deliver_to_agent
                            from .models import ChatMessage
                            for member in grp.members:
                                if member.identity_uri == identity:
                                    continue
                                fanout_msg = ChatMessage(
                                    sender=identity, recipient=member.identity_uri,
                                    content=reply, thread_id=gid,
                                )
                                if local_deliver_to_agent(fanout_msg):
                                    continue
                                try:
                                    transport.send_message(fanout_msg)
                                except Exception as fanout_exc:
                                    logger.warning(
                                        "group fan-out to %s failed: %s",
                                        member.identity_uri, fanout_exc,
                                    )
                            self.advocacy_responses += 1
                        else:
                            logger.warning(
                                "group responder: group %s not found for reply", gid
                            )
                except Exception as exc:
                    logger.warning("group responder failed: %s", exc)
                    self._log(f"Group responder error: {exc}", "warning")
                return  # handled; don't also run DM advocacy/plugins
            if group_responder is not None and not (
                (msg.content or "").lstrip().startswith("<event ")
            ):
                try:
                    dm_reply = group_responder.respond_direct(msg)
                    if dm_reply:
                        from .models import ChatMessage
                        transport.send_message(
                            ChatMessage(sender=identity, recipient=msg.sender, content=dm_reply)
                        )
                        self.advocacy_responses += 1
                        return  # handled; skip advocacy/plugins
                except Exception as dm_exc:
                    logger.warning("direct responder failed: %s", dm_exc)
            if engine:
                try:
                    reply = engine.process_message(msg)
                    if reply:
                        transport.send_and_store(msg.sender, reply)
                        self.advocacy_responses += 1
                except Exception as exc:
                    logger.warning("daemon.py: %s", exc)
                    self._log(f"Advocacy error: {exc}", "warning")
            if plugin_registry:
                for plugin in plugin_registry.get_plugins():
                    if plugin.should_handle(msg):
                        try:
                            plugin_reply = plugin.handle(msg)
                            if plugin_reply:
                                transport.send_and_store(msg.sender, plugin_reply)
                        except Exception as exc:
                            logger.warning("daemon.py: %s", exc)
                            self._log(f"Plugin '{plugin.name}' error: {exc}", "warning")
```

Then replace the poll-loop per-message body (current lines 406-532) so it reads:

```python
                        for msg in messages:
                            if self._route_file_message(msg):
                                continue
                            _process(msg)
```

(The `self.total_received += len(messages)` and `Received N message(s)` log at lines 394-397 stay unchanged, above the loop.)

- [ ] **Step 3: Run the daemon suites — same green baseline as Step 1**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon.py tests/test_daemon_group.py -q`
Expected: PASS, identical passed count to Step 1 (behavior unchanged by the extraction).

- [ ] **Step 4: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/daemon.py
git commit -m "refactor(daemon): extract per-message dispatch into _process() closure

Behavior-preserving extraction of the inline poll-loop dispatch (group/DM/
advocacy/plugins) into a start()-local _process(msg); continue->return. Prep
for moving generation off the poll thread.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X2Uu2UH6c7rhdrbNMwsz2L"
```

---

### Task 2: Move generation onto a single worker thread (async), lock sends, drain on stop

Poll loop enqueues instead of calling `_process` inline; one worker thread drains the queue; `transport.send_message`/`send_and_store` guarded by a lock; `stop()` drains and joins the worker.

**Files:**
- Modify: `src/skchat/daemon.py` — module import; `__init__` (add `_genqueue`/`_genworker`/`_send_lock`); `start()` (worker closure + start it, poll loop enqueues, `_process` sends under lock); `stop()` (drain + join).
- Test: `tests/test_daemon_async.py` (new), `tests/test_daemon_integration.py` (add drain before `advocacy_responses` asserts).

**Interfaces:**
- Consumes: `_process(msg)` closure from Task 1.
- Produces: `self._genqueue: queue.Queue`, `self._send_lock: threading.Lock`, `self._genworker: Optional[threading.Thread]`; the worker closure `_gen_worker()`; `drain(timeout: float | None = None) -> None` on `ChatDaemon` (calls `self._genqueue.join()`).

- [ ] **Step 1: Write the failing async test — poll loop keeps polling while generation blocks**

Create `tests/test_daemon_async.py`:

```python
import threading
import time
from unittest.mock import MagicMock, patch

from skchat.daemon import ChatDaemon
from skchat.models import ChatMessage


def _dm(content="hello", sender="capauth:chef@skworld.io"):
    return ChatMessage(sender=sender, recipient="capauth:lumina@skworld.io", content=content)


def test_poll_loop_does_not_block_on_generation(monkeypatch):
    """While one reply is generating (held), the poll loop keeps polling and the
    worker eventually answers — proving generation is off the poll thread."""
    daemon = ChatDaemon(interval=0.01, quiet=True)

    gate = threading.Event()          # holds the DM responder inside generation
    sent = []

    # transport: first poll returns one DM, subsequent polls return []
    polls = [[_dm("first")]]
    transport = MagicMock()
    transport.poll_inbox.side_effect = lambda: polls.pop(0) if polls else []
    transport.send_message.side_effect = lambda m: sent.append(m)

    # group_responder: not a group message; respond_direct blocks on the gate
    responder = MagicMock()
    def slow_direct(msg):
        gate.wait(timeout=5)
        return "answered"
    responder.respond_direct.side_effect = slow_direct

    monkeypatch.setattr("skchat.daemon.SKComms", MagicMock())
    with patch("skchat.daemon.ChatTransport") as CT, \
         patch("skchat.daemon.ChatHistory") as CH, \
         patch.object(ChatDaemon, "_init_subsystems_bg_marker", create=True):
        CT.from_config.return_value = transport
        CH.from_config.return_value = MagicMock()
        # Inject the responder + identity that _process closes over by patching the
        # subsystem init to set them. Simplest: run start() in a thread, then set
        # the closure's group_responder via the documented test seam below.
        t = threading.Thread(target=daemon.start, daemon=True)
        t.start()
        # Wait until the daemon installed the async worker + responder seam.
        deadline = time.time() + 3
        while time.time() < deadline and daemon._genworker is None:
            time.sleep(0.01)
        # Force the responder into the running daemon (test seam, see impl note).
        daemon._test_set_group_responder(responder, agent="lumina")

        # Poll loop must advance past the first poll while generation is gated.
        deadline = time.time() + 3
        while time.time() < deadline and daemon.poll_count < 3:
            time.sleep(0.01)
        assert daemon.poll_count >= 3, "poll loop blocked on generation"
        assert sent == [], "reply sent before gate released"

        gate.set()                    # release generation
        daemon.drain(timeout=5)       # wait for the worker to finish the job
        assert any(m.content == "answered" for m in sent)

        daemon.running = False
        t.join(timeout=3)
```

Implementer note on the test seam: `_process`/`group_responder` are `start()`-locals, so add a tiny test-only setter used above. In `start()`, after declaring the subsystem locals, assign `self._set_group_responder = lambda r, agent=None: ...` is awkward across the closure boundary; instead expose it cleanly: store the responder-setter as `self._test_set_group_responder` that reassigns the `nonlocal group_responder` (and `group_cfg`). Add inside `start()`:

```python
        def _test_set_group_responder(r, agent="lumina"):
            nonlocal group_responder, group_cfg
            from .group_responder import load_group_config
            group_cfg = load_group_config(agent)
            group_responder = r
        self._test_set_group_responder = _test_set_group_responder
```

This is a deliberate, documented test seam (a bound attribute reassigning the closure's `nonlocal`s), acceptable because the async behavior can only be exercised through a live `start()`.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon_async.py -q`
Expected: FAIL — `AttributeError: 'ChatDaemon' object has no attribute '_genworker'` (or `_test_set_group_responder`), since async plumbing does not exist yet.

- [ ] **Step 3: Add the module import and `__init__` fields**

At the top of `src/skchat/daemon.py`, with the other stdlib imports (near `import threading`, line 20), add:

```python
from queue import Empty, Queue
```

In `ChatDaemon.__init__` (after `self._attachment_service: Optional[object] = None`, line 180), add:

```python
        # Async generation: the poll loop enqueues received messages; a single
        # worker thread drains this FIFO and runs the blocking generate→send→
        # store chain, so polling never stalls on the ~10s LLM call.
        self._genqueue: "Queue" = Queue()
        self._genworker: Optional[threading.Thread] = None
        # Serialize transport.send_message across the worker + the poll thread's
        # outbox flush (skcomms send is not audited for concurrent use).
        self._send_lock = threading.Lock()
```

- [ ] **Step 4: Add `drain()` on `ChatDaemon`**

Add a method (near `stop()`):

```python
    def drain(self, timeout: Optional[float] = None) -> None:
        """Block until the generation worker has processed all queued messages.

        Used by tests and clean shutdown so replies aren't lost mid-flight.
        """
        self._genqueue.join()
```

- [ ] **Step 5: Add the worker closure, start it, enqueue in the poll loop, lock sends**

In `start()`:

(a) Guard every `transport.send_message(...)` / `transport.send_and_store(...)` call inside the `_process` closure with the lock. Change each such call to:

```python
                                try:
                                    with self._send_lock:
                                        transport.send_message(fanout_msg)
```
```python
                        with self._send_lock:
                            transport.send_message(
                                ChatMessage(sender=identity, recipient=msg.sender, content=dm_reply)
                            )
```
```python
                        with self._send_lock:
                            transport.send_and_store(msg.sender, reply)
```
```python
                                with self._send_lock:
                                    transport.send_and_store(msg.sender, plugin_reply)
```

(b) After defining `_process` (and `_test_set_group_responder`), define and start the worker:

```python
        def _gen_worker() -> None:
            """Drain the generation queue FIFO, one message at a time (ordered
            replies, no intra-agent concurrency)."""
            while self.running or not self._genqueue.empty():
                try:
                    msg = self._genqueue.get(timeout=1.0)
                except Empty:
                    continue
                try:
                    _process(msg)
                except Exception as exc:  # one bad job never kills the worker
                    logger.warning("genworker: processing failed: %s", exc)
                finally:
                    self._genqueue.task_done()

        self._genworker = threading.Thread(
            target=_gen_worker, daemon=True, name="skchat-genworker"
        )
        self._genworker.start()
```

(c) Change the poll-loop per-message body from `_process(msg)` to enqueue:

```python
                        for msg in messages:
                            if self._route_file_message(msg):
                                continue
                            self._genqueue.put(msg)
```

- [ ] **Step 6: Drain + join the worker in `stop()`**

In `stop()`, after `self.running = False` is set (find the existing line), add:

```python
        # Let the generation worker finish in-flight + queued replies, then join.
        if self._genworker is not None and self._genworker.is_alive():
            try:
                self._genqueue.join()
            except Exception:
                pass
            self._genworker.join(timeout=10)
```

- [ ] **Step 7: Run the async test — passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon_async.py -q`
Expected: PASS (poll_count advanced while gated; reply delivered after drain).

- [ ] **Step 8: Update integration tests to drain before asserting `advocacy_responses`**

In `tests/test_daemon_integration.py`, for each test that asserts on `daemon.advocacy_responses` (generation count), insert `daemon.drain(timeout=5)` after the message is received and before the assert. Do NOT change `total_received` assertions (receive-time, unchanged).

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon_integration.py -q -m 'not e2e_live'`
Expected: PASS.

- [ ] **Step 9: Full daemon suite green**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon.py tests/test_daemon_group.py tests/test_daemon_async.py tests/test_group_responder.py -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/daemon.py tests/test_daemon_async.py tests/test_daemon_integration.py
git commit -m "feat(daemon): async generation — poll loop enqueues, single worker drains

Poll loop no longer blocks on the ~10s generate() call: it enqueues received
messages onto a FIFO; one skchat-genworker thread drains it and runs the
respond→send→store chain. transport.send_message serialized via _send_lock;
stop() drains+joins the worker; drain() added for deterministic tests. Ordered
replies, no thread-pool races. Fixes back-to-back messages getting missed.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X2Uu2UH6c7rhdrbNMwsz2L"
```

---

## Notes for the executor

- After both tasks, a live smoke test (optional, not required by the plan): `systemctl --user restart skchat-daemon skchat-daemon-opus`, then send two rapid `@all` messages <10s apart and confirm both agents answer both — the pre-fix failure mode. Space is not required anymore.
- The `_test_set_group_responder` seam is test-only; leave it (harmless, and documents the closure boundary). If a reviewer objects, the alternative is promoting the 7 dispatch locals to `self._*` attributes set in `_init_subsystems_bg` — larger diff, same effect.
