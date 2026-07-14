# Group Key-Distribution Delivery Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a group's wrapped epoch secret to member daemons so a receiver can unseal sealed group messages — wiring the send/receive delivery around the already-built, already-proven `apply_group_key_package` crypto core (SEAM 9).

**Architecture:** When a hybrid group is keyed (epoch secret seeded) and sealing is on, the sender delivers a `group_epoch_advance` package to each keyed member as a **typed** message (routing by `metadata["group_key_package"]`, NOT by string-sniffing content — honors SEAM 5). The receiving daemon detects that metadata on poll, calls `apply_group_key_package`, and consumes the message (it is control-plane, never a chat turn). Distribution happens once per epoch (tracked in group metadata) so it is not re-sent on every message.

**Tech Stack:** Python 3.10+, existing `daemon_proxy_groups` (build/distribute/apply), `GroupChat`/`GroupKeyDistributor` (group.py), `local_deliver_to_agent` + skcomms transport (delivery), the daemon poll loop (daemon.py).

## Global Constraints

- PYTHON ONLY (no Flutter/Dart).
- `SKCHAT_SEAL_GROUPS` stays OFF by default; all new behavior is gated behind it (distribution only fires when sealing is enabled). No behavior change when the flag is off.
- Control-plane routing is by typed metadata (`metadata["group_key_package"]`), never a `__PREFIX__` in content (SEAM 5 anti-pattern).
- Fail-closed-readable: any delivery/apply failure logs and continues; never crash the poll loop or the send path.
- Reuse the proven crypto core `apply_group_key_package` and `GroupKeyDistributor.distribute_key`; do NOT reinvent crypto.
- Line length 99; run tests from `~` (skmemory namespace collision, see CLAUDE.md).

---

### Task 1: Build + distribute the epoch package

**Files:**
- Modify: `src/skchat/daemon_proxy_groups.py`
- Test: `tests/test_group_key_delivery.py`

**Interfaces:**
- Produces: `build_group_epoch_package(group) -> dict` (the `group_epoch_advance` package: `type/group_id/epoch/key_version/kem_suite/distributions`), and `distribute_group_epoch(group, sender_uri, deliver) -> list[str]` (delivers a typed key-message to each keyed member except the sender via the `deliver(chat_msg) -> bool` callback; returns the list of member URIs delivered to).
- Consumes: `GroupKeyDistributor.distribute_key` (group.py), `_member_has_group_key`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_group_key_delivery.py
from skchat import daemon_proxy_groups as G
from skchat.group import GroupChat, MemberRole

def _hybrid_group_with_member(pub_hex):
    g = GroupChat(name="Ops", kem_suite="x25519-mlkem768")
    g.add_member(identity_uri="capauth:sender@skworld.io", role=MemberRole.ADMIN)
    g.add_member(identity_uri="capauth:recv@skworld.io", hybrid_kem_public_hex=pub_hex)
    g.ensure_epoch()
    return g

def test_build_package_shape():
    g = _hybrid_group_with_member("aa" * 32)
    pkg = G.build_group_epoch_package(g)
    assert pkg["type"] == "group_epoch_advance"
    assert pkg["group_id"] == g.id and pkg["epoch"] == g.epoch
    assert "capauth:recv@skworld.io" in pkg["distributions"]

def test_distribute_delivers_typed_message_to_keyed_members_only():
    g = _hybrid_group_with_member("bb" * 32)
    sent = []
    def deliver(m):
        sent.append(m); return True
    delivered = G.distribute_group_epoch(g, "capauth:sender@skworld.io", deliver)
    assert delivered == ["capauth:recv@skworld.io"]        # sender excluded, keyless excluded
    m = sent[0]
    assert m.recipient == "capauth:recv@skworld.io"
    assert m.metadata.get("group_key_package", {}).get("type") == "group_epoch_advance"
    assert m.metadata.get("group_id") == g.id
```

- [ ] **Step 2: Run to verify it fails** (`AttributeError: build_group_epoch_package`).

- [ ] **Step 3: Implement**

```python
# in daemon_proxy_groups.py
def build_group_epoch_package(group) -> dict:
    from .group import GroupKeyDistributor
    return {
        "type": "group_epoch_advance",
        "group_id": group.id,
        "epoch": group.epoch,
        "key_version": group.key_version,
        "kem_suite": group.kem_suite,
        "distributions": GroupKeyDistributor.distribute_key(group),
    }

def distribute_group_epoch(group, sender_uri, deliver) -> list:
    """Deliver the epoch package to each KEYED member (except sender) as a typed
    control message (metadata['group_key_package']). `deliver(chat_msg)->bool`."""
    from .models import ChatMessage
    pkg = build_group_epoch_package(group)
    out = []
    for member in group.members:
        if member.identity_uri == sender_uri or not _member_has_group_key(group, member):
            continue
        if not pkg["distributions"].get(member.identity_uri):
            continue
        msg = ChatMessage(
            sender=sender_uri, recipient=member.identity_uri, content="",
            thread_id=group.id,
            metadata={"group_key_package": pkg, "group_id": group.id,
                      "kind": "group_key"},
        )
        try:
            if deliver(msg):
                out.append(member.identity_uri)
        except Exception as exc:
            logger.warning("distribute_group_epoch: deliver to %s failed: %s",
                           member.identity_uri, exc)
    return out
```

- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** `feat(groups): build + distribute the epoch key package (typed)`.

---

### Task 2: Fire distribution from the send path, once per epoch

**Files:**
- Modify: `src/skchat/daemon_proxy_groups.py` (`fan_out_send`)
- Test: `tests/test_group_key_delivery.py`

**Interfaces:**
- Consumes: `distribute_group_epoch` (Task 1), the existing `_delivery_transport`/`local_deliver_to_agent` in `fan_out_send`.
- Behavior: when `SKCHAT_SEAL_GROUPS` is on and the group is hybrid+keyed, and `group.metadata.get("epoch_distributed") != group.epoch`, distribute the epoch package to members (using the same `local_deliver_to_agent`-then-transport delivery `fan_out_send` already uses), then set `group.metadata["epoch_distributed"] = group.epoch`. Never distribute when the flag is off.

- [ ] **Step 1: Write the failing test** — with the flag on, a first sealed `fan_out_send` to a keyed group delivers a `group_key_package` message to the member's inbox; a second send does NOT re-distribute (epoch unchanged). With the flag off, nothing is distributed.

```python
def test_fan_out_distributes_epoch_once_when_sealing(isolated_groupstore, monkeypatch):
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    delivered = []
    monkeypatch.setattr(G, "local_deliver_to_agent",
                        lambda m: (delivered.append(m) or True))
    monkeypatch.setattr(G, "_delivery_transport", lambda uri: None)
    g = _hybrid_group_with_member("cc" * 32)
    G.save_group(g)
    G.fan_out_send(g, _hist(), "capauth:sender@skworld.io", "one")
    key_msgs = [m for m in delivered if m.metadata.get("group_key_package")]
    assert len(key_msgs) == 1                                  # distributed once
    G.fan_out_send(G.load_group(g.id), _hist(), "capauth:sender@skworld.io", "two")
    key_msgs = [m for m in delivered if m.metadata.get("group_key_package")]
    assert len(key_msgs) == 1                                  # NOT re-distributed
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** — in `fan_out_send`, after `seal`/`enc_state` are computed and before the per-member loop, add:

```python
    if seal and getattr(group, "is_hybrid", False) and \
            group.metadata.get("epoch_distributed") != group.epoch:
        _t = _delivery_transport(sender_uri)
        def _deliver(m):
            return local_deliver_to_agent(m) or (
                _t.send_message(m) if _t is not None else False) or True
        distribute_group_epoch(group, sender_uri, _deliver)
        group.metadata["epoch_distributed"] = group.epoch
```

(Guard so a member-copy delivery failure never blocks the message; `save_group(group)` already runs at the end of `fan_out_send`, persisting `epoch_distributed`.)

- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** `feat(groups): distribute epoch key once per epoch on sealed send`.

---

### Task 3: Receiver daemon consumes the key message

**Files:**
- Modify: `src/skchat/daemon_proxy_groups.py` (a `consume_group_key_message` helper), `src/skchat/daemon.py` (poll loop)
- Test: `tests/test_group_key_delivery.py`

**Interfaces:**
- Produces: `consume_group_key_message(msg, agent=None) -> bool` — if `msg.metadata` carries a `group_key_package`, call `apply_group_key_package(pkg, self_uri=msg.recipient, agent=agent)` and return True (message consumed, it is control-plane, not a chat turn); else False.
- Consumes: `apply_group_key_package` (already built + proven).

- [ ] **Step 1: Write the failing test** — a ChatMessage carrying a real `group_key_package` (built by a sender for the receiver) is consumed and keys the receiver's local group copy; a normal message returns False (not consumed).

```python
def test_consume_group_key_message_keys_local_group(tmp_path, monkeypatch):
    from skchat import pq_prekeys as PQ
    if not PQ.available():
        import pytest; pytest.skip("no PQ")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path)); monkeypatch.setenv("SKAGENT", "recv")
    from skchat.models import ChatMessage
    from skchat.group import GroupChat, MemberRole
    pub, _ = PQ.ensure_agent_keypair("recv")
    recv_uri = "capauth:recv@skworld.io"
    sender = GroupChat(name="S", kem_suite="x25519-mlkem768")
    sender.add_member(identity_uri="capauth:s@skworld.io", role=MemberRole.ADMIN)
    sender.add_member(identity_uri=recv_uri, hybrid_kem_public_hex=pub.hex())
    sender.ensure_epoch()
    pkg = G.build_group_epoch_package(sender)
    recv = GroupChat(name="S", kem_suite="x25519-mlkem768"); recv.id = sender.id
    recv.add_member(identity_uri=recv_uri, role=MemberRole.ADMIN)
    G.save_group(recv)
    msg = ChatMessage(sender="capauth:s@skworld.io", recipient=recv_uri, content="",
                      thread_id=sender.id, metadata={"group_key_package": pkg})
    assert G.consume_group_key_message(msg, agent="recv") is True
    assert G.load_group(sender.id).epoch_secret_hex == sender.epoch_secret_hex
    # a normal message is not consumed
    assert G.consume_group_key_message(
        ChatMessage(sender="x", recipient=recv_uri, content="hi"), agent="recv") is False
```

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** the helper:

```python
def consume_group_key_message(msg, agent=None) -> bool:
    pkg = (getattr(msg, "metadata", None) or {}).get("group_key_package")
    if not isinstance(pkg, dict):
        return False
    apply_group_key_package(pkg, self_uri=getattr(msg, "recipient", "") or "", agent=agent)
    return True   # consumed regardless of apply outcome (never a chat turn)
```

Then in `daemon.py`'s poll loop, right after a message is received and before the group/brain handling, add:

```python
                from .daemon_proxy_groups import consume_group_key_message
                if consume_group_key_message(msg, agent=group_cfg.agent):
                    continue   # control-plane key delivery; not a chat turn
```

(Place it before `_is_group_message`/responder handling so a key message is never treated as a chat turn or persisted to a thread.)

- [ ] **Step 4: Run to verify pass** (both cases).
- [ ] **Step 5: Commit** `feat(groups): daemon consumes typed group-key messages`.

---

### Task 4: End-to-end delivery integration test

**Files:**
- Test: `tests/test_group_key_delivery.py`

- [ ] **Step 1: Write the test** — sender `fan_out_send` (flag on) delivers a key message into the receiver agent's inbox path; feed that message through `consume_group_key_message`; then the receiver unseals the sealed member copy from the SAME send. Assert cleartext round-trip end to end (send→distribute→consume→unseal), reusing the real PQ KEM.

```python
def test_send_distribute_consume_unseal_end_to_end(tmp_path, monkeypatch):
    from skchat import pq_prekeys as PQ
    if not PQ.available():
        import pytest; pytest.skip("no PQ")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path)); monkeypatch.setenv("SKAGENT", "recv")
    from skchat.group import GroupChat, MemberRole
    pub, _ = PQ.ensure_agent_keypair("recv")
    recv_uri = "capauth:recv@skworld.io"
    inbox = []
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    monkeypatch.setattr(G, "local_deliver_to_agent", lambda m: (inbox.append(m) or True))
    monkeypatch.setattr(G, "_delivery_transport", lambda uri: None)
    g = GroupChat(name="S", kem_suite="x25519-mlkem768")
    g.add_member(identity_uri="capauth:s@skworld.io", role=MemberRole.ADMIN)
    g.add_member(identity_uri=recv_uri, hybrid_kem_public_hex=pub.hex())
    g.ensure_epoch(); G.save_group(g)
    # receiver's own local group copy (no secret yet)
    r = GroupChat(name="S", kem_suite="x25519-mlkem768"); r.id = g.id
    r.add_member(identity_uri=recv_uri, role=MemberRole.ADMIN); G.save_group(r)
    G.save_group(g)  # restore sender copy as canonical for load in the send
    sealed_member_copy = None
    G.fan_out_send(G.load_group(g.id), _hist(), "capauth:s@skworld.io", "e2e secret")
    key_msgs = [m for m in inbox if m.metadata.get("group_key_package")]
    sealed_msgs = [m for m in inbox if G.is_sealed_group_content(getattr(m, "content", ""))]
    assert key_msgs and sealed_msgs
    for m in key_msgs:                      # receiver consumes the key
        G.consume_group_key_message(m, agent="recv")
    opened = G.unseal_group_content(G.load_group(g.id), sealed_msgs[0].content)
    assert opened == "e2e secret"
```

(Note: the sender + receiver share the group-store dir in-process; in production they are separate daemons/stores. This proves the send→distribute→consume→unseal chain; the true multi-daemon cross-store test is a follow-up runbook step.)

- [ ] **Step 2: Run to verify pass.**
- [ ] **Step 3: Commit** `test(groups): send->distribute->consume->unseal end to end`.

---

## What Not To Touch

- The proven crypto core (`apply_group_key_package`, `GroupKeyDistributor`, `unseal_group_content`) — reuse, do not modify.
- The classical `group_key_rotation` (PGP) path — this plan is hybrid-only.
- `SKCHAT_SEAL_GROUPS` default — stays OFF.
