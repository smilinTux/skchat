# Single-Writer Message Log Implementation Plan (SEAM 2/4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** A single-writer, append-only, per-conversation message log with a server-assigned immutable `message_id` and a monotonic `seq`, plus idempotent writes keyed by an optional `client_dedup_key`. This is the authoritative-store foundation (SEAM 2) + the message-id authority (SEAM 4). Additive + isolated in a NEW module; wired into the send path behind `SKCHAT_MESSAGE_LOG` (default OFF) so it CANNOT change existing behavior until enabled.

**Architecture:** A new `src/skchat/message_log.py` owns a SQLite/WAL table `message_log(conversation_id, seq, message_id, client_dedup_key, sender, recipient, content, ts, kind)`. `seq` is assigned atomically per `conversation_id` inside a single transaction (the single writer). `append()` is idempotent: a repeat `client_dedup_key` (or `message_id`) returns the existing row, never a second seq. `read(conversation_id, since_seq)` returns ordered rows. JSONL/existing history stay untouched; this is a parallel authoritative projection that later becomes the source of truth.

**Tech Stack:** Python 3.10+, stdlib `sqlite3` (WAL mode), existing `models.ChatMessage`.

## Global Constraints
- PYTHON ONLY. New module only + a flag-gated call site; do NOT modify existing history.py write paths.
- `SKCHAT_MESSAGE_LOG` default OFF: when off, nothing new runs, zero behavior change.
- Single writer: `seq` assignment MUST be atomic per conversation (one transaction, `INSERT ... SELECT COALESCE(MAX(seq),0)+1`). No race can hand two messages the same seq.
- Idempotent: same `client_dedup_key` (when provided) OR same `message_id` -> return the existing row, no new seq, no duplicate.
- The DB path honors `SKCHAT_HOME` (reuse `history._skchat_home()` or mirror it); create the parent dir before connect (LaneStore-style).
- Line length 99; run tests from `~`.

---

### Task 1: MessageLog core (append + seq + idempotency)
**Files:** Create `src/skchat/message_log.py`; Test `tests/test_message_log.py`.
**Interfaces:**
- Produces: `class MessageLog(db_path)`, `append(conversation_id, *, message_id=None, client_dedup_key=None, sender, recipient, content, kind="text") -> dict` (returns `{conversation_id, seq, message_id, deduped: bool, ...}`; assigns `message_id` if None), `read(conversation_id, since_seq=0, limit=500) -> list[dict]`, `latest_seq(conversation_id) -> int`.

- [ ] **Step 1: Write failing tests**
```python
def test_append_assigns_monotonic_seq(tmp_path):
    log = MessageLog(str(tmp_path/"m.db"))
    a = log.append("c1", sender="s", recipient="r", content="one")
    b = log.append("c1", sender="s", recipient="r", content="two")
    assert a["seq"] == 1 and b["seq"] == 2
    assert a["message_id"] and a["message_id"] != b["message_id"]

def test_seq_is_per_conversation(tmp_path):
    log = MessageLog(str(tmp_path/"m.db"))
    assert log.append("c1", sender="s", recipient="r", content="x")["seq"] == 1
    assert log.append("c2", sender="s", recipient="r", content="y")["seq"] == 1

def test_client_dedup_key_is_idempotent(tmp_path):
    log = MessageLog(str(tmp_path/"m.db"))
    a = log.append("c1", client_dedup_key="k1", sender="s", recipient="r", content="hi")
    b = log.append("c1", client_dedup_key="k1", sender="s", recipient="r", content="hi again")
    assert b["seq"] == a["seq"] and b["deduped"] is True   # no second seq
    assert log.latest_seq("c1") == 1

def test_read_returns_ordered_since_seq(tmp_path):
    log = MessageLog(str(tmp_path/"m.db"))
    for i in range(3): log.append("c1", sender="s", recipient="r", content=str(i))
    rows = log.read("c1", since_seq=1)
    assert [r["seq"] for r in rows] == [2, 3]
```
- [ ] **Step 2:** Run -> fail (no module).
- [ ] **Step 3:** Implement `message_log.py` — WAL SQLite, `mkdir` parent before connect, UNIQUE(conversation_id, client_dedup_key) and UNIQUE(message_id); `append` in one transaction does the dedup lookup then `INSERT ... seq = COALESCE(MAX(seq),0)+1 WHERE conversation_id=?`. On a UNIQUE(client_dedup_key) hit, SELECT + return `deduped=True`.
- [ ] **Step 4:** Run -> pass.
- [ ] **Step 5:** Commit `feat(message-log): single-writer append + monotonic seq + idempotent dedup`.

### Task 2: Concurrency safety (single-writer under load)
**Files:** `tests/test_message_log.py`
- [ ] **Step 1:** Write a test that fires N concurrent `append` calls (threads) for the same conversation and asserts the seqs are exactly `1..N` with no gaps or duplicates (the single-writer invariant).
- [ ] **Step 2:** Run -> if it fails, wrap the append in `BEGIN IMMEDIATE` / a process lock so seq assignment is serialized; re-run -> pass.
- [ ] **Step 3:** Commit `test(message-log): concurrent appends get unique contiguous seqs`.

### Task 3: Flag-gated shadow-write from the send path
**Files:** `src/skchat/daemon_proxy.py` (`api_send`, after `_persist` of the user + reply); Test `tests/test_daemon_proxy_lumina.py`.
- [ ] **Step 1:** Write a failing test: with `SKCHAT_MESSAGE_LOG=1`, a send ALSO appends the message to the MessageLog (assert `latest_seq` advanced); with the flag off, the log is untouched.
- [ ] **Step 2:** Run -> fail.
- [ ] **Step 3:** Implement `_shadow_log(msg)` guarded by `os.getenv("SKCHAT_MESSAGE_LOG")` truthiness; call it best-effort (try/except, never break the send) after persisting a message. It's a SHADOW write — the JSONL/SQLite path is unchanged and authoritative for now.
- [ ] **Step 4:** Run -> pass; confirm flag-off leaves all existing api_send tests unchanged.
- [ ] **Step 5:** Commit `feat(daemon_proxy): flag-gated shadow-write to the single-writer log`.

## What Not To Touch
- Existing history.py write paths, JSONL, the group/DM stores. This log is additive + parallel.
- `SKCHAT_MESSAGE_LOG` stays OFF; making the log the source of truth (retiring JSONL) is a later phase.
