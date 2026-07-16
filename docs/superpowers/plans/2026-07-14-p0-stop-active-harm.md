# P0 Stop-Active-Harm (crypto fail-closed + control-frame stopgap) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is an independently reviewable coord unit; the SKOS Autopilot may execute one task per agent in an isolated worktree (PR-only, no auto-merge).

**Goal:** Stop the three active-harm behaviours the architecture review flagged as P0: (1) cleartext leaking when an encrypted conversation cannot be sealed, (2) a missing *signing* key wrongly disabling *confidentiality*, and (3) control frames still reaching the LLM brain.

**Architecture:** Surgical, refactor-in-place changes to the existing app path (`daemon_proxy.api_send`) and the transport path (`ChatTransport.send`). No new subsystems. Every change is fail-closed and additive; none alters the wire format or the client contract (that is a later plan). The control-frame change is explicitly a **throwaway stopgap** that the P3 typed-control-plane plan will delete.

**Tech Stack:** Python 3.10+, FastAPI, pytest. Repo: `skchat` (`~/clawd/skcapstone-repos/skchat`). Run tests from `~` to avoid the skmemory namespace collision: `cd ~ && ~/.skenv/bin/python -m pytest skcapstone-repos/skchat/tests/... ` (or with the repo on PYTHONPATH).

## Global Constraints

- Line length 99 (black + ruff, ignore E501). Target Python 3.10+.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`
- NEVER use em/en dashes in code comments, docstrings, or commit messages (house rule). Regular hyphens only.
- Fail-closed means: on an encrypted conversation with no usable key, the message is **refused or queued, never emitted as cleartext**. A refusal surfaces to the caller as an explicit error/status; it must NOT 500 silently or fall through to plaintext.
- No behaviour change for the classical (non-hybrid, non-ratchet) path: an unencrypted conversation still sends plaintext exactly as today.
- Tests must not touch real infra (no real skmem-pg, no real network). Use tmp dirs / monkeypatched seal-open helpers.

---

### Task 1: Fail closed when a hybrid reply cannot be sealed (app path)

**Files:**
- Modify: `src/skchat/daemon_proxy.py:1272-1288` (the reply-seal block inside `api_send`)
- Test: `tests/test_daemon_proxy_lumina.py`

**Interfaces:**
- Consumes: existing `_seal_hybrid_outbound(plaintext, *, recipient_short) -> str | None` (daemon_proxy.py:485), `convo_is_hybrid: bool` (set true at daemon_proxy.py:1111-1116 when the inbound was a `pqdm1:` token, i.e. the peer negotiated encryption), `_persist(...)`, the `JSONResponse` return shape `{ok, id, recipient, ts, reply}`.
- Produces: no new public symbol. Behaviour contract: when `convo_is_hybrid` is true and `_seal_hybrid_outbound` returns `None`, `api_send` returns HTTP 503 `{ok: false, error: "reply_not_sealable", detail: ...}` and does NOT persist or return a plaintext reply.

**Current defect (verbatim, daemon_proxy.py:1273-1277):** `reply_wire = reply_text; if convo_is_hybrid: sealed = _seal_hybrid_outbound(...); if sealed is not None: reply_wire = sealed`. When `sealed is None`, `reply_wire` stays plaintext and is persisted + returned on an encrypted conversation. That is the cleartext leak.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_daemon_proxy_lumina.py`. Drive `api_send` for a Lumina recipient with an inbound `pqdm1:` token (so `convo_is_hybrid` is true), monkeypatch `_seal_hybrid_outbound` to return `None` (simulating a missing peer prekey), and a stub brain that returns a fixed reply.

```python
import json, pytest
from starlette.requests import Request
from skchat import daemon_proxy as dp

def _req(body: dict) -> Request:
    payload = json.dumps(body).encode()
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}
    return Request({"type": "http", "method": "POST", "headers": []}, receive)

@pytest.mark.asyncio
async def test_hybrid_reply_that_cannot_seal_fails_closed(monkeypatch, tmp_path):
    # conversation is hybrid: _open_hybrid_inbound succeeds -> convo_is_hybrid True
    monkeypatch.setattr(dp, "_open_hybrid_inbound", lambda tok, *, sender_short: "hi lumina")
    # sealing the outbound reply is impossible (no peer prekey) -> None
    monkeypatch.setattr(dp, "_seal_hybrid_outbound", lambda txt, *, recipient_short: None)
    class _Brain:
        def reply(self, *a, **k): return "here is my plaintext answer"
    monkeypatch.setattr(dp, "_get_brain", lambda: _Brain())
    # isolate history to tmp so the test never writes real data
    monkeypatch.setattr(dp, "_get_history", lambda: _FakeHistory(tmp_path))  # see helper below

    resp = await dp.api_send(_req({"recipient": "capauth:lumina@skworld.io",
                                   "content": "pqdm1:HYBRID:QUJD"}))
    assert resp.status_code == 503
    payload = json.loads(resp.body)
    assert payload["ok"] is False
    assert payload["error"] == "reply_not_sealable"
    # and no plaintext reply row was persisted
    hist = dp._get_history()
    assert not any(m.content == "here is my plaintext answer" for m in hist.saved)
```

Provide the minimal `_FakeHistory` helper in the test file (records `.saved`, supports `.save`, `.load(peer=...)` returning `[]`, `.record_receipt`). If a lighter existing fixture exists in this test module, reuse it instead of adding one.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && SKCHAT_HOME=$PWD/_t ~/.skenv/bin/python -m pytest skcapstone-repos/skchat/tests/test_daemon_proxy_lumina.py -k fails_closed -v`
Expected: FAIL (currently returns 200 with a plaintext reply persisted).

- [ ] **Step 3: Write the minimal implementation**

Replace the seal block in `api_send` so a failed seal on a hybrid conversation refuses instead of leaking plaintext:

```python
        reply_wire = reply_text
        if convo_is_hybrid:
            sealed = _seal_hybrid_outbound(reply_text, recipient_short="chef")
            if sealed is None:
                # FAIL CLOSED: the peer negotiated encryption but we cannot seal
                # the reply (no/expired peer prekey, backend down). Never emit the
                # plaintext on an encrypted conversation. Do NOT persist the reply.
                logger.warning(
                    "api_send: refusing to send an UNSEALED reply on a hybrid "
                    "conversation (recipient=chef); returning 503"
                )
                return JSONResponse(
                    {"ok": False, "error": "reply_not_sealable",
                     "detail": "encrypted conversation, reply could not be sealed"},
                    status_code=503,
                )
            reply_wire = sealed
```

(The `user_msg` for Chef's inbound turn was already persisted above; that is correct, only the un-sealable *reply* is withheld. The dedup cache MUST NOT cache a 503 as a reply, ensure the `_SEND_RECENT[...] = (now, _result)` line is only reached on success, i.e. it stays after this block.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~ && SKCHAT_HOME=$PWD/_t ~/.skenv/bin/python -m pytest skcapstone-repos/skchat/tests/test_daemon_proxy_lumina.py -k fails_closed -v`
Expected: PASS.

- [ ] **Step 5: Regression — the classical (non-hybrid) path is unchanged**

Add/confirm a test: an inbound plain-text message (no `pqdm1:`) still gets a normal 200 plaintext reply persisted. Run the full module: `pytest skcapstone-repos/skchat/tests/test_daemon_proxy_lumina.py -v`. Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/skchat/daemon_proxy.py tests/test_daemon_proxy_lumina.py
git commit -m "fix(daemon_proxy): fail closed when a hybrid reply cannot be sealed

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Route __REACT__ and __EDIT__ control frames away from the brain (stopgap)

**Files:**
- Modify: `src/skchat/daemon_proxy.py:1122-1148` (the existing control-frame short-circuit block in `api_send`)
- Test: `tests/test_daemon_proxy_lumina.py`

**Interfaces:**
- Consumes: the existing `content.startswith("__TYPING__")` / `content.startswith("__RECEIPT__")` short-circuit block.
- Produces: `api_send` returns `{ok: true, control: "reaction"}` / `{ok: true, control: "edit"}` for `__REACT__`/`__EDIT__` content, and does NOT call the brain or persist a chat turn for them.

**Note (must be in the code comment):** this is a THROWAWAY STOPGAP. Prefix-sniffing control frames is the anti-pattern the P3 typed-control-plane plan removes. Do not extend this pattern further than the four known sentinels; flag it as temporary.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel,control", [("__REACT__", "reaction"), ("__EDIT__", "edit")])
async def test_control_frames_do_not_reach_brain(monkeypatch, sentinel, control):
    called = {"brain": False}
    class _Brain:
        def reply(self, *a, **k):
            called["brain"] = True
            return "should never be generated"
    monkeypatch.setattr(dp, "_get_brain", lambda: _Brain())
    resp = await dp.api_send(_req({"recipient": "capauth:lumina@skworld.io",
                                   "content": sentinel + ':{"target_id":"x"}'}))
    assert resp.status_code == 200
    assert json.loads(resp.body)["control"] == control
    assert called["brain"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest skcapstone-repos/skchat/tests/test_daemon_proxy_lumina.py -k control_frames -v`
Expected: FAIL (brain is invoked; `control` key absent).

- [ ] **Step 3: Write the minimal implementation**

Extend the control-frame block (after the existing `__RECEIPT__` handler) with reaction/edit handling that is out-of-band. `__REACT__` should apply the reaction if the history layer supports it (best-effort), `__EDIT__` should apply the edit best-effort; both return without a brain call:

```python
    if content.startswith("__REACT__"):
        # STOPGAP: reactions ride as message content today. Apply best-effort,
        # never brain-reply. (P3 replaces sentinel-sniffing with typed envelopes.)
        try:
            import json as _json
            payload = _json.loads(content.split(":", 1)[1]) if ":" in content else {}
            tid, emoji = payload.get("target_id"), payload.get("emoji")
            if tid and emoji and hasattr(_get_history(), "add_reaction"):
                _get_history().add_reaction(tid, emoji, OPERATOR_ID)
        except Exception:
            logger.debug("reaction handling failed", exc_info=True)
        return JSONResponse({"ok": True, "control": "reaction"})
    if content.startswith("__EDIT__"):
        # STOPGAP: edits ride as message content today. Apply best-effort, no brain.
        try:
            import json as _json
            payload = _json.loads(content.split(":", 1)[1]) if ":" in content else {}
            tid, new = payload.get("target_id"), payload.get("content")
            if tid and new is not None and hasattr(_get_history(), "edit_message"):
                _get_history().edit_message(tid, new, OPERATOR_ID)
        except Exception:
            logger.debug("edit handling failed", exc_info=True)
        return JSONResponse({"ok": True, "control": "edit"})
```

If `add_reaction`/`edit_message` do not exist on the history object, the best-effort guard skips them, the point of THIS task is only that control frames never reach the brain, not to build reaction storage.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest skcapstone-repos/skchat/tests/test_daemon_proxy_lumina.py -k control_frames -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skchat/daemon_proxy.py tests/test_daemon_proxy_lumina.py
git commit -m "fix(daemon_proxy): route __REACT__/__EDIT__ control frames away from the brain (stopgap)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Decouple confidentiality from signing + fail-closed transport ratchet

**Files:**
- Modify: `src/skchat/transport.py:312-344` (`_dm_ratchet_manager`) and the `ChatTransport.send` encrypt/degrade path
- Modify: `src/skchat/crypto.py:783-825` (`load_agent_crypto` degradation semantics) — telemetry only, no behaviour change to loading
- Test: `tests/test_transport_federation.py` (or a new `tests/test_transport_failclosed.py`)

**Interfaces:**
- Consumes: `ChatTransport(skcomms, history, identity, crypto=...)`, `_dm_ratchet_manager()` (returns a manager or `None`), the `SKCHAT_DM_RATCHET` env gate.
- Produces: a transport-level policy: when a conversation is ratchet-encrypted (`SKCHAT_DM_RATCHET` on AND a manager exists for a peer with an established session) but the specific send cannot be sealed, `send` raises `ConfidentialityError` (new, in transport.py) instead of falling back to a classical plaintext envelope. Signing degradation (missing PGP key) is orthogonal and only sets a `signing_degraded` flag surfaced by `/health` + `/api/v1/status`.

**Interfaces produced (exact):** `class ConfidentialityError(Exception)` in `transport.py`; `ChatTransport.signing_degraded: bool` attribute; both consumed by the health endpoint task below.

- [ ] **Step 1: Write the failing test (fail-closed ratchet send)**

Construct a `ChatTransport` with a stub skcomms and a stub crypto whose ratchet manager is present for the peer but whose `encrypt`/`seal` raises. With `SKCHAT_DM_RATCHET=1`, assert `send(peer, content)` raises `ConfidentialityError` and that NO envelope was handed to skcomms (no plaintext leak).

```python
def test_ratchet_send_fails_closed(monkeypatch):
    monkeypatch.setenv("SKCHAT_DM_RATCHET", "1")
    sk = _StubSkcomms()  # records .sent envelopes
    t = ChatTransport(skcomms=sk, history=_FakeHistory(), identity="capauth:lumina@skworld.io",
                      crypto=_StubCrypto(seal_raises=True))
    # force a manager that claims an established session but fails to seal
    monkeypatch.setattr(t, "_dm_ratchet_manager", lambda: _StubMgr(has_session=True, seal_raises=True))
    with pytest.raises(transport.ConfidentialityError):
        t.send(recipient="chef@skworld.io", content="secret")
    assert sk.sent == []  # nothing left the box in cleartext
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest skcapstone-repos/skchat/tests/test_transport_failclosed.py -k fails_closed -v`
Expected: FAIL (today it swallows the seal error and degrades to a classical plaintext envelope).

- [ ] **Step 3: Write the minimal implementation**

In `ChatTransport.send`: when a ratchet manager exists AND reports an established session for the peer, sealing failure must raise `ConfidentialityError`, not fall through. Keep the classical path for peers with NO negotiated ratchet unchanged (a peer who never negotiated encryption is not "encrypted", plaintext is correct there). Add:

```python
class ConfidentialityError(RuntimeError):
    """Raised when an encrypted conversation cannot be sealed. Fail closed:
    never fall back to a cleartext envelope for a peer with a live ratchet."""
```

Guard the seal call site accordingly (exact lines to be located in `send`; the manager exposes whether a session exists for the peer). Do not change behaviour for the classical/no-ratchet path.

- [ ] **Step 4: Run to verify pass + no regression on the classical path**

Run: `pytest skcapstone-repos/skchat/tests/test_transport_federation.py skcapstone-repos/skchat/tests/test_transport_failclosed.py -v`
Expected: all PASS (classical peer still sends plaintext; ratcheted peer fails closed).

- [ ] **Step 5: Signing-degradation telemetry (separate from confidentiality)**

Add `signing_degraded` (bool) to `ChatTransport`, set true when `load_agent_crypto` returns None while `SKCHAT_DM_RATCHET` intent is present. Surface it in `/health` and `/api/v1/status`. Add a test asserting a missing PGP key sets `signing_degraded=True` but does NOT disable the ratchet (confidentiality) when a hybrid key is present.

- [ ] **Step 6: Commit**

```bash
git add src/skchat/transport.py src/skchat/crypto.py tests/test_transport_failclosed.py
git commit -m "fix(transport): fail closed on unsealable ratchet send; decouple signing degradation from confidentiality

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** P0.1 (fail-closed missing key) -> Tasks 1 (app path) + 3 (transport path). P0.2 (decouple confidentiality from signing + degradation flag) -> Task 3 steps 5. P0.3b (control-frame -> brain leak, extend to `__REACT__`/`__EDIT__`) -> Task 2. P0.4 (disable duplicate responders) is ops, handled directly outside this plan. P0.5/P0.6 (data-plane CapAuth + versioned dual-serve) are a separate follow-up plan (they can lock out the live app and need the capability handshake) — explicitly out of scope here.
- **Placeholder scan:** the exact `send`-site line numbers in Task 3 step 3 are "to be located" because `send` is long; the executor must grep the seal call in `send` (the manager + `has_session` check), this is a locate-then-edit, not a placeholder for the logic, which is fully specified (raise `ConfidentialityError` instead of classical fallback for a peer with a live session).
- **Type consistency:** `ConfidentialityError`, `signing_degraded`, `{ok, error, detail}` 503 shape, and `{ok, control}` 200 shape are used consistently across tasks and the health endpoint.

## Execution Handoff

Route each task to the coord board as its own item tagged `repo:skchat`, then run the SKOS Autopilot in **canary / PR-only** mode (`skos autopilot run --task <id> --no-dry-run --canary`), one task per isolated worktree, graded to 5/5, returned as a PR for Chef to approve. `automerge_repos` stays empty. Order: Task 1 -> Task 2 -> Task 3 (Task 3 is the largest; run it last once the pipeline is proven on 1 and 2).
