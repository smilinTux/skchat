# Calling Backend Agent-to-Agent (First Real 1:1 Call) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a real 1:1 voice call ring and be answered with two-way audio, agent-to-agent (Lumina calls Opus), over the existing `/call/*` LiveKit path.

**Architecture:** One config drift (peer store realm `skworld` vs cluster realm `skworld.io`) causes both the 404 (`_resolve_peer`) and the 500 (skcomms `_load_recipient_key` `same_box`). Re-keying the peer store to `skworld.io` fixes both at the source. Then: harden `same_box` against future drift, teach `_resolve_peer` to accept the `capauth:` URI, and build one minimal always-on auto-answer service that polls `/call/incoming` and joins the LiveKit room.

**Tech Stack:** Python (skchat FastAPI call_routes, skcomms mailbox, a new answerer service), systemd user units, capauth identity, LiveKit. `pytest`.

## Global Constraints

- **No em/en dashes** anywhere (code, comments, docs, commit messages). Commas, colons, parentheses, new sentences. Regular hyphens fine.
- **Python style:** line length 99, ruff (E, W, F, I; ignore E501). Run server tests FROM `~` (skmemory namespace collision): `cd ~ && ~/.skenv/bin/python -m pytest <path> -q`.
- **Security invariants (must not regress):** the operator-token gate stays closed (`SKCHAT_GUEST_OPERATOR_TOKEN` + `SKCHAT_DATAPLANE_AUTH=1`); the answerer PRESENTS the token, never reintroduce a loopback/tailnet bypass. At-rest sealing stays fail-closed (`_seal_for_recipient` raising on a missing key is correct). Do NOT widen the operator-own-key fallback to mask a missing key. `/call/incoming` signature + anti-spoof checks stay.
- **The realm re-key is fleet-wide shared state** (`~/.skcapstone/skcomms/*` syncs across nodes). Apply consistently across every syncing node, or a partial re-key strands a node.
- **Canonical realm decision (approved):** re-key UP to `skworld.io` (peer store is stale; cluster.json is authoritative).

## Reference: exact code at the edit sites (verbatim, current)

skcomms `mailbox.py:_load_recipient_key` same_box computation:
```python
    if "@" in to_fqid:
        agent, suffix = to_fqid.split("@", 1)
        same_box = suffix == f"{get_operator()}.{get_realm()}"
    else:
        agent = to_fqid
        same_box = True
```

skchat `call_routes.py:_resolve_peer`:
```python
def _resolve_peer(peer: str) -> str:
    """Resolve a peer arg (FQID or bare name) to a paired FQID, or 404."""
    peers = _list_peers()
    if peer in peers:
        return peer
    matches = [fqid for fqid in peers if fqid.split("@", 1)[0] == peer]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"ambiguous bare name {peer!r}: matches {matches}; use full FQID",
        )
    raise HTTPException(status_code=404, detail=f"peer not paired: {peer}")
```

## File Structure

- `~/.skcapstone/skcomms/peers.json`, `~/.skcapstone/skcomms/known_fingerprints.json` (RE-KEY, config; fleet-wide)
- `~/.skcapstone/skcomms/peers/*` (DELETE debris)
- `skcomms/src/skcomms/mailbox.py` (MODIFY `_load_recipient_key` same_box) + `skcomms/tests/` (test)
- `skchat/src/skchat/call_routes.py` (MODIFY `_resolve_peer`) + `skchat/tests/test_call_routes.py` (integration test)
- `skchat/src/skchat/daemon_proxy.py:42` (`LUMINA_ID` canonical form)
- `scripts/call_answerer.py` (CREATE, the auto-answer service; repo TBD in Task 6) + a systemd unit
- `~/.config/systemd/user/skchat-call-answerer@.service` (CREATE) + disable `skchat-lumina-call.service`

---

## Task 1: Re-key the peer store to `skworld.io` (fixes 404 + 500 at the source)

**Files:**
- Modify: `~/.skcapstone/skcomms/peers.json`
- Modify: `~/.skcapstone/skcomms/known_fingerprints.json` (if present, same re-key)

**Interfaces:** none (config). Produces: `list_peers()` keys and `_load_recipient_key` lookups keyed by `<agent>@chef.skworld.io`.

- [ ] **Step 1: Snapshot + find every consumer of the old string**

```bash
cp ~/.skcapstone/skcomms/peers.json ~/.skcapstone/skcomms/peers.json.bak-realmrekey
cat ~/.skcapstone/skcomms/peers.json
grep -rn "@chef.skworld\b" ~/.skcapstone/ 2>/dev/null | grep -v '\.io' | head -40
grep -rn "chef\.skworld[^.]" ~/clawd/skcapstone-repos/skchat/src ~/clawd/skcapstone-repos/skcomms/src 2>/dev/null | grep -v '\.io' | head -20
```
Record every hit. Any file that hardcodes `@chef.skworld` (no `.io`) must be re-keyed too (e.g. `daemon_proxy.py` LUMINA_ID, Task 5).

- [ ] **Step 2: Verify the current failure (baseline)**

```bash
cd ~ && ~/.skenv/bin/python -c "
from skcomms.peers import list_peers
from skcomms.mailbox import _load_recipient_key
print('keys:', list(list_peers()))
print('load .io  ->', 'FOUND' if _load_recipient_key('opus@chef.skworld.io') else 'None')
print('load stale->', 'FOUND' if _load_recipient_key('opus@chef.skworld') else 'None')
"
```
Expected (baseline): keys are the stale `*@chef.skworld`; `.io` load is `None` (the bug); stale load is `FOUND` (bare-suffix accident). This documents the failure.

- [ ] **Step 3: Re-key the store**

Rewrite each top-level key in `~/.skcapstone/skcomms/peers.json` from
`<agent>@chef.skworld` to `<agent>@chef.skworld.io`, preserving each value
object verbatim (`syncthing_device_id`, `fingerprint`, `added_at`). Do the same
for any `<agent>@chef.skworld` keys in `known_fingerprints.json`. Use a script,
not hand-editing, so the value objects are preserved exactly:

```bash
cd ~ && ~/.skenv/bin/python - <<'PY'
import json, pathlib
for name in ("peers.json", "known_fingerprints.json"):
    p = pathlib.Path.home() / ".skcapstone" / "skcomms" / name
    if not p.exists():
        continue
    d = json.loads(p.read_text())
    out = {}
    for k, v in d.items():
        nk = k
        if "@" in k:
            agent, suffix = k.split("@", 1)
            if suffix == "chef.skworld":
                nk = f"{agent}@chef.skworld.io"
        out[nk] = v
    p.write_text(json.dumps(out, indent=2) + "\n")
    print(name, "->", list(out))
PY
```

- [ ] **Step 4: Verify the fix**

```bash
cd ~ && ~/.skenv/bin/python -c "
from skcomms.peers import list_peers
from skcomms.mailbox import _load_recipient_key
print('keys:', list(list_peers()))
print('load .io ->', 'FOUND' if _load_recipient_key('opus@chef.skworld.io') else 'None')
print('load lumina.io ->', 'FOUND' if _load_recipient_key('lumina@chef.skworld.io') else 'None')
"
```
Expected: keys are now `*@chef.skworld.io`; both `.io` loads return `FOUND` (the 500 is fixed at the source; `same_box` now passes because the suffix matches `chef.skworld.io`).

- [ ] **Step 5: Apply fleet-wide, then commit the snapshot note**

The store syncs across nodes. Apply the SAME re-key on every node that carries
`~/.skcapstone/skcomms/peers.json` (run Step 3 there, or let Syncthing propagate
and confirm each node shows `.io` keys with `list_peers()`). Do NOT leave a node
on the old keys. This is config, not a repo commit; record what was changed and
which nodes were updated in the report.

---

## Task 2: Remove migration debris

**Files:**
- Delete: `~/.skcapstone/skcomms/peers/lumina.pub.asc` (literal `"fakekey"`), `~/.skcapstone/skcomms/peers/Lumina.yml` (fabricated fingerprint, case-dup), and any stale no-pubkey `*.skworld.io` / `lumina-box@` TOFU entries the swarm found.

**Interfaces:** none.

- [ ] **Step 1: Identify the debris**

```bash
ls -la ~/.skcapstone/skcomms/peers/
grep -rl "fakekey" ~/.skcapstone/skcomms/peers/ 2>/dev/null
cd ~ && ~/.skenv/bin/python -c "
from skcomms.peers import list_peers
for k, v in list_peers().items():
    print(k, '->', 'fp:' + str(v.get('fingerprint'))[:12])
"
```
Confirm which entries are placeholders (fakekey / fabricated fingerprint / no real pubkey). Do NOT delete a real paired peer.

- [ ] **Step 2: Snapshot + remove**

```bash
mkdir -p ~/.skcapstone/skcomms/peers/.debris-bak
mv ~/.skcapstone/skcomms/peers/lumina.pub.asc ~/.skcapstone/skcomms/peers/.debris-bak/ 2>/dev/null || true
mv ~/.skcapstone/skcomms/peers/Lumina.yml ~/.skcapstone/skcomms/peers/.debris-bak/ 2>/dev/null || true
```
Remove stale `*.skworld.io`-no-pubkey and `lumina-box@` TOFU entries from `peers.json` (edit the JSON, keep the two real re-keyed agents).

- [ ] **Step 3: Verify no placeholder resolves**

```bash
cd ~ && ~/.skenv/bin/python -c "
from skcomms.mailbox import _load_recipient_key
k = _load_recipient_key('lumina@chef.skworld.io')
assert k and 'fakekey' not in k, 'placeholder key still resolving'
print('OK: lumina key is real, no fakekey')
"
```
Expected: PASS. Record the removed entries in the report.

---

## Task 3: Harden the skcomms `same_box` check (defense in depth)

**Files:**
- Modify: `skcomms/src/skcomms/mailbox.py` (`_load_recipient_key` same_box)
- Test: `skcomms/tests/test_mailbox_recipient_key.py` (CREATE, or add to the existing mailbox test)

**Interfaces:**
- Consumes: `get_operator()` (already imported in mailbox.py).
- Produces: `_load_recipient_key` resolves a same-box agent's local key by matching the OPERATOR component only, so a realm-string drift can never strand it, while a cross-operator name collision is still rejected.

- [ ] **Step 1: Write the failing test**

Create `skcomms/tests/test_mailbox_recipient_key.py`:

```python
"""_load_recipient_key must resolve a same-box agent's local key even when the
peer fqid's realm string has drifted from the cluster realm (operator-component
match), while still rejecting a cross-operator name collision."""
import skcomms.mailbox as M


def test_same_box_match_is_operator_component_not_exact_realm(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "get_operator", lambda: "chef")
    monkeypatch.setattr(M, "get_realm", lambda: "skworld.io")

    # A local agent key on disk, addressed by bare agent name.
    key_dir = tmp_path / "agents" / "lumina" / "capauth" / "identity"
    key_dir.mkdir(parents=True)
    (key_dir / "public.asc").write_text("REAL-LUMINA-KEY")
    monkeypatch.setattr(
        M, "_agent_identity_dir", lambda a: tmp_path / "agents" / a / "capauth" / "identity"
    )
    monkeypatch.setattr(M, "skcomms_home", lambda: tmp_path / "home")

    # Drifted realm on the fqid (chef.skworld, cluster says chef.skworld.io):
    # operator component still "chef", so the local key MUST resolve.
    assert M._load_recipient_key("lumina@chef.skworld") == "REAL-LUMINA-KEY"
    # Current realm form also resolves.
    assert M._load_recipient_key("lumina@chef.skworld.io") == "REAL-LUMINA-KEY"


def test_cross_operator_name_collision_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "get_operator", lambda: "chef")
    monkeypatch.setattr(M, "get_realm", lambda: "skworld.io")
    key_dir = tmp_path / "agents" / "lumina" / "capauth" / "identity"
    key_dir.mkdir(parents=True)
    (key_dir / "public.asc").write_text("LOCAL-LUMINA-KEY")
    monkeypatch.setattr(
        M, "_agent_identity_dir", lambda a: tmp_path / "agents" / a / "capauth" / "identity"
    )
    monkeypatch.setattr(M, "skcomms_home", lambda: tmp_path / "home")

    # A DIFFERENT operator's agent that happens to be named "lumina" must NOT
    # seal to the local lumina key (no peers/<fqid>.asc exists -> None).
    assert M._load_recipient_key("lumina@stranger.otherrealm") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skcomms/tests/test_mailbox_recipient_key.py -q`
Expected: the first test FAILS on the `lumina@chef.skworld` (drifted) assertion (exact-realm `same_box` is False, so the local key is skipped and it returns None).

- [ ] **Step 3: Write minimal implementation**

In `skcomms/src/skcomms/mailbox.py:_load_recipient_key`, replace the same_box computation:

```python
    if "@" in to_fqid:
        agent, suffix = to_fqid.split("@", 1)
        same_box = suffix == f"{get_operator()}.{get_realm()}"
    else:
        agent = to_fqid
        same_box = True
```
with an operator-component match (realm-string drift no longer strands a local key; a cross-operator name collision is still rejected):

```python
    if "@" in to_fqid:
        agent, suffix = to_fqid.split("@", 1)
        # Match on the OPERATOR component only, not the exact operator.realm
        # string. The threat model is a cross-operator agent-name collision
        # (lumina@stranger.otherrealm on a box with local lumina), which the
        # operator prefix still rejects. A realm rename (skworld -> skworld.io)
        # must NOT strand a local agent's own key, which the exact-string
        # compare did (see the calling-backend design doc).
        operator_component = suffix.split(".", 1)[0]
        same_box = operator_component == get_operator()
    else:
        agent = to_fqid
        same_box = True
```

Add a debug log just before `return None` at the end of the function to distinguish "same_box rejected" from "no key anywhere" (use the module's existing `logger`):
```python
    logger.debug(
        "no recipient key for %s (same_box=%s, tried %d candidates)",
        to_fqid, same_box, len(candidates),
    )
    return None
```
(If the module has no `logger`, add `import logging` + `logger = logging.getLogger(__name__)` at the top.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skcomms/tests/test_mailbox_recipient_key.py -q`
Expected: PASS (2 tests). Also run the existing mailbox suite to confirm no regression: `~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skcomms/tests/ -q -k mailbox`.

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skcomms
git add src/skcomms/mailbox.py tests/test_mailbox_recipient_key.py
git commit -m "fix(mailbox): match same-box recipient key on operator component, not exact realm"
```

---

## Task 4: Un-monkeypatched integration test of the real seal seam

**Files:**
- Test: `skchat/tests/test_call_routes_integration.py` (CREATE)

**Interfaces:** exercises the real `list_peers` + `resolve_agent_identity` + `send_message` chain (the seam both live failures hit), which the existing `test_call_routes.py` monkeypatches away.

- [ ] **Step 1: Write the test**

Create `skchat/tests/test_call_routes_integration.py`. It must run against the real re-keyed store (Task 1) and confirm `/call/start`'s send path seals successfully for a same-box paired agent, without monkeypatching `_send_invite`/`_list_peers`/`_self_fqid`. Mark it so it can be skipped where the real `~/.skcapstone` store is absent:

```python
"""Integration: the real _resolve_peer + skcomms send_message + sealing seam
that the monkeypatched unit tests skip. Requires a real re-keyed peer store."""
import pytest

pytestmark = pytest.mark.integration

from skchat import call_routes as CR


def _store_ready() -> bool:
    try:
        peers = CR._list_peers()
    except Exception:
        return False
    return any(k.endswith("skworld.io") for k in peers)


@pytest.mark.skipif(not _store_ready(), reason="real re-keyed peer store not present")
def test_prepare_call_resolves_and_seals_for_same_box_agent():
    # Resolve a paired same-box agent (opus) by its real fqid.
    fqid = next(k for k in CR._list_peers() if k.split("@", 1)[0] == "opus")
    resolved = CR._resolve_peer(fqid)
    assert resolved == fqid
    # The recipient key must load (this is what threw the live 500).
    from skcomms.mailbox import _load_recipient_key
    assert _load_recipient_key(resolved), "recipient key must resolve after re-key"
```

- [ ] **Step 2: Run it**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_call_routes_integration.py -q -m integration`
Expected: PASS after Task 1 (or SKIP if the real store is absent in this environment). If it FAILS, the re-key (Task 1) is incomplete.

- [ ] **Step 3: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add tests/test_call_routes_integration.py
git commit -m "test(call_routes): integration test of the real resolve + seal seam"
```

---

## Task 5: `_resolve_peer` accepts the `capauth:` URI + canonical `LUMINA_ID`

**Files:**
- Modify: `skchat/src/skchat/call_routes.py` (`_resolve_peer`)
- Modify: `skchat/src/skchat/daemon_proxy.py:42` (`LUMINA_ID`)
- Test: `skchat/tests/test_call_routes.py` (add cases)

**Interfaces:**
- Produces: `_resolve_peer` resolves any of: an exact paired fqid, a bare agent name, a `capauth:<agent>@<domain>` wire URI, or a differently-realmed fqid, all to the paired fqid (via bare-agent-name match). `LUMINA_ID` is the re-keyed canonical form.

- [ ] **Step 1: Write the failing test**

Add to `skchat/tests/test_call_routes.py` (it already monkeypatches `_list_peers`; reuse that pattern):

```python
def test_resolve_peer_accepts_capauth_uri(monkeypatch):
    from skchat import call_routes as CR
    monkeypatch.setattr(CR, "_list_peers", lambda: {"opus@chef.skworld.io": {}})
    # capauth wire URI the Flutter client sends resolves to the paired fqid.
    assert CR._resolve_peer("capauth:opus@skworld.io") == "opus@chef.skworld.io"
    # bare name still works
    assert CR._resolve_peer("opus") == "opus@chef.skworld.io"
    # exact fqid still works
    assert CR._resolve_peer("opus@chef.skworld.io") == "opus@chef.skworld.io"
    # a differently-realmed fqid resolves by bare name too
    assert CR._resolve_peer("opus@chef.skworld") == "opus@chef.skworld.io"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_call_routes.py -q -k capauth`
Expected: FAIL (the current `_resolve_peer` 404s on `capauth:opus@skworld.io`).

- [ ] **Step 3: Write minimal implementation**

Replace `_resolve_peer` in `call_routes.py` with (keep the exact-match fast path, then reduce any other form to the bare agent name and match):

```python
def _resolve_peer(peer: str) -> str:
    """Resolve a peer arg to a paired FQID, or raise 404/409.

    Canonical `peer` accepted (in priority): an exact paired FQID; otherwise
    reduce to the bare agent name and match, so a bare name, a
    ``capauth:<agent>@<domain>`` wire URI (what the Flutter client sends), and a
    differently-realmed FQID all resolve to the same paired agent. Bare-name
    ambiguity across operators raises 409.
    """
    peers = _list_peers()
    if peer in peers:
        return peer
    probe = peer[len("capauth:"):] if peer.startswith("capauth:") else peer
    bare = probe.split("@", 1)[0]
    matches = [fqid for fqid in peers if fqid.split("@", 1)[0] == bare]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"ambiguous peer {peer!r}: matches {matches}; use full FQID",
        )
    raise HTTPException(status_code=404, detail=f"peer not paired: {peer}")
```

In `daemon_proxy.py:42`, change `LUMINA_ID` from `"lumina@chef.skworld"` to the re-keyed canonical `"lumina@chef.skworld.io"` (grep first to confirm the exact current value and that nothing else depends on the old string).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_call_routes.py -q`
Expected: PASS (the new capauth cases + the existing call_routes suite, no regression).

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/call_routes.py src/skchat/daemon_proxy.py tests/test_call_routes.py
git commit -m "feat(call_routes): resolve capauth: URIs + realm-agnostic peer match; canonical LUMINA_ID"
```

---

## Task 6: Minimal auto-answer service

**Files:**
- Create: `skchat/scripts/call_answerer.py` (the always-on answerer)
- Test: `skchat/tests/test_call_answerer.py`

**Interfaces:**
- Consumes: `GET /call/incoming` and `POST /call/answer` (sending `X-Operator-Token`); LiveKit connect/publish machinery reused from `lumina-creative/scripts/lumina-call.py`.
- Produces: `call_answerer.py` exposing a testable core: `poll_and_answer(api, seen: set) -> Optional[dict]` that (a) GETs `/call/incoming`, (b) picks the newest un-seen verified invite (dedupe by `nonce`), (c) POSTs `/call/answer {peer: <invite.from_fqid>}`, (d) returns `{room, token, livekit_url}` to hand to the LiveKit join. The network layer is behind a small `AnswererApi` seam so the test injects a fake (no live HTTP, no LiveKit).

- [ ] **Step 1: Read the reuse points FIRST**

Before writing, read `lumina-creative/scripts/lumina-call.py` for its LiveKit connect + audio-publish functions (the swarm cited `mint_token()` around line 2972 and the connect machinery). The answerer REUSES the connect/publish, driven by the DYNAMIC room from `/call/answer`, NOT `lumina-call.py`'s hardcoded `lumina-and-chef` room. Note the exact function names you will call; record them in the report.

- [ ] **Step 2: Write the failing test (the pure poll/answer core)**

Create `skchat/tests/test_call_answerer.py`:

```python
from skchat.scripts_call_answerer import poll_and_answer  # see Step 4 import note


class _FakeApi:
    def __init__(self, invites):
        self._invites = invites
        self.answered = []
    def poll_incoming(self):
        return self._invites
    def answer(self, peer):
        self.answered.append(peer)
        return {"room": "call-abc", "token": "tok", "livekit_url": "wss://sfu"}


def test_answers_newest_unseen_invite():
    api = _FakeApi([
        {"from_fqid": "lumina@chef.skworld.io", "room": "call-abc",
         "livekit_url": "wss://sfu", "nonce": "n1", "ts": 5},
    ])
    seen = set()
    res = poll_and_answer(api, seen)
    assert api.answered == ["lumina@chef.skworld.io"]
    assert res["room"] == "call-abc" and res["token"] == "tok"
    assert "n1" in seen


def test_dedupes_by_nonce():
    api = _FakeApi([
        {"from_fqid": "lumina@chef.skworld.io", "room": "r",
         "livekit_url": "w", "nonce": "n1", "ts": 5},
    ])
    seen = {"n1"}
    res = poll_and_answer(api, seen)
    assert res is None and api.answered == []


def test_no_invites_is_noop():
    api = _FakeApi([])
    assert poll_and_answer(api, set()) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_call_answerer.py -q`
Expected: FAIL (module missing).

- [ ] **Step 4: Write minimal implementation**

Create `skchat/scripts/call_answerer.py` with (a) the pure core and (b) the runnable loop. The pure core:

```python
"""Minimal always-on 1:1 call answerer.

Polls GET /call/incoming (presenting X-Operator-Token), and on a fresh
signature-verified invite calls POST /call/answer to get the LiveKit room +
token, then joins that DYNAMIC room and publishes audio (LiveKit join reused
from lumina-creative/scripts/lumina-call.py). Consumes only invites the server
has already anti-spoof-verified; never re-parses unverified bodies.
"""
from __future__ import annotations

from typing import Optional


def poll_and_answer(api, seen: set) -> Optional[dict]:
    """One poll cycle: answer the newest un-seen invite. Returns the
    {room, token, livekit_url} to join, or None. Pure over the `api` seam."""
    invites = api.poll_incoming() or []
    fresh = [i for i in invites if i.get("nonce") and i["nonce"] not in seen]
    if not fresh:
        return None
    fresh.sort(key=lambda i: i.get("ts", 0), reverse=True)
    invite = fresh[0]
    seen.add(invite["nonce"])
    joinable = api.answer(invite["from_fqid"])
    return {
        "room": joinable["room"],
        "token": joinable["token"],
        "livekit_url": joinable["livekit_url"],
    }
```

The `AnswererApi` (real HTTP, out of the test path): a small class wrapping
`requests`/`httpx` with the base URL + `X-Operator-Token` header on every call,
`poll_incoming()` -> `GET /call/incoming` `["invites"]`, `answer(peer)` ->
`POST /call/answer {peer}`. The runnable `main()`: loop `poll_and_answer` every
~3s; when it returns a joinable, call the LiveKit connect/publish from
lumina-call.py with that room/token/url. Read the operator token + webui URL from
env (`SKCHAT_GUEST_OPERATOR_TOKEN`, `SKCHAT_WEBUI_URL`).

Import note: expose the test import as `skchat.scripts_call_answerer` via a thin
shim, OR place `call_answerer.py` where the test can import it and adjust the
test's import line to match; keep the pure `poll_and_answer` importable without
pulling in LiveKit deps (guard the LiveKit import inside `main()`).

- [ ] **Step 5: Run test to verify it passes + commit**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_call_answerer.py -q`
Expected: PASS (3 tests).
```bash
cd ~/clawd/skcapstone-repos/skchat
git add scripts/call_answerer.py tests/test_call_answerer.py
git commit -m "feat(call): minimal 1:1 auto-answer service (poll incoming -> answer -> join dynamic room)"
```

---

## Task 7: Systemd unit for the answerer; disable the crash-loop

**Files:**
- Create: `~/.config/systemd/user/skchat-call-answerer@.service` (templated per agent)
- Config: disable `skchat-lumina-call.service`

**Interfaces:** none (deployment).

- [ ] **Step 1: Stop the crash-loop**

```bash
systemctl --user disable --now skchat-lumina-call.service
systemctl --user status skchat-lumina-call.service | head -3
```
Expected: inactive, no restart storm. Record the ~20k-restart context in the report.

- [ ] **Step 2: Write the answerer unit**

Create `~/.config/systemd/user/skchat-call-answerer@.service` running
`call_answerer.py` as the callee agent (instance `%i`, e.g. `opus`), with
`Environment=SKAGENT=%i`, `SKCHAT_GUEST_OPERATOR_TOKEN=...` (from the same env
file the webui uses, do NOT inline the secret in the unit; use
`EnvironmentFile=`), `SKCHAT_WEBUI_URL=http://localhost:8765`,
`Restart=on-failure`, `RestartSec=5`.

- [ ] **Step 3: Start + verify it polls**

```bash
systemctl --user daemon-reload
systemctl --user enable --now skchat-call-answerer@opus.service
sleep 6
systemctl --user status skchat-call-answerer@opus.service | head -5
journalctl --user -u skchat-call-answerer@opus.service -n 20 --no-pager | tail
```
Expected: active, logs show it polling `/call/incoming` without 401 (it presents the token). No restart storm.

- [ ] **Step 4: Commit the unit into the repo systemd tree (if the repo tracks units)**

If skchat tracks units under `systemd/`, add a templated copy there per the
repo's convention (secrets externalized). Record the live unit path in the report.

---

## Task 8: End-to-end acceptance (Lumina calls Opus)

**Files:** none (acceptance).

- [ ] **Step 1: Place the call from Lumina to Opus via the server path**

With the answerer running as opus, ring from lumina (the webui runs as lumina,
so `_self_fqid()` is lumina, the caller):
```bash
TOK=$(grep SKCHAT_GUEST_OPERATOR_TOKEN ~/.config/skchat/webui-lumina.env | cut -d= -f2)
curl -s -X POST http://localhost:8765/call/start \
  -H 'Content-Type: application/json' -H "X-Operator-Token: $TOK" \
  -d '{"peer":"opus@chef.skworld.io"}' | head -c 400
```
Expected: a 200 with `{room, token, livekit_url, peer_fqid, identity}` (NOT a 404 and NOT a 500). The sealing succeeded (invite delivered).

- [ ] **Step 2: Confirm the answerer joined**

```bash
journalctl --user -u skchat-call-answerer@opus.service --since "1 min ago" --no-pager | tail -15
```
Expected: the answerer logged an incoming invite, called `/call/answer`, and connected to the derived `call-<hash>` room. Confirm both parties are in the same room (the LiveKit room name from Step 1 == the answerer's joined room).

- [ ] **Step 3: Confirm two-way audio + record the result**

Verify (via the LiveKit server participant list or the answerer/caller logs) that
both lumina and opus are publishing audio tracks in the derived room. This is the
card's acceptance. Record the outcome (room name, both participants, audio
published) in the report. If audio is one-way or a party did not join, capture
the log and STOP (do not claim success).

---

## Self-Review

**1. Spec coverage:**
- Re-key peer store to `.io` (fixes 404 + 500) -> Task 1. ✅
- Clean migration debris -> Task 2. ✅
- Harden `same_box` -> Task 3. ✅
- Un-monkeypatched integration test of the seal seam -> Task 4. ✅
- `_resolve_peer` accepts `capauth:` URI + canonical `LUMINA_ID` -> Task 5. ✅
- Minimal auto-answer service -> Task 6. ✅
- Systemd unit + disable lumina-call crash-loop -> Task 7. ✅
- End-to-end Lumina calls Opus, two-way audio -> Task 8. ✅
- Deferred Option A (you call Lumina) -> spec section 8, not in this plan. ✅

**2. Placeholder scan:** Config tasks give exact scripts/commands with expected
output; code tasks give complete edits and tests. Task 6's LiveKit join reuse
is explicitly a "read lumina-call.py first, record the function names" step
(Step 1) because the exact connect/publish API is in that file, not quotable
blind; the pure `poll_and_answer` core is fully specified and tested.

**3. Type/consistency:** the canonical fqid `<agent>@chef.skworld.io` is used
consistently across Tasks 1, 3, 4, 5, 8. `poll_and_answer(api, seen) -> dict|None`
is defined in Task 6 and its return shape (`{room, token, livekit_url}`) matches
`/call/answer`'s response used in Task 8. The `same_box` operator-component match
(Task 3) is consistent with the re-key (Task 1) making the exact match pass too.

**Risk flags for the implementer:** (a) Task 1 is fleet-wide shared state, apply
on every syncing node or a node strands. (b) Task 6's LiveKit reuse requires
reading lumina-call.py; if its connect API cannot be cleanly reused for a dynamic
room, report it before forking a large amount of code. (c) `known_fingerprints.json`
may not exist or may use a different key shape than `peers.json`, inspect before
re-keying (Task 1 Step 3 guards with `if p.exists()` but confirm the key shape).
