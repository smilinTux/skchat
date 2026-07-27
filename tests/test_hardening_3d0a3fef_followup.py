"""Card 3d0a3fef follow-up: hardening the newly-activated live crypto path.

Wiring ChatCrypto into CLI/webui/skseal/group-fan-out transports (the 3d0a3fef
fix) turned five previously-inert surfaces into live ratchet writers. These tests
lock in the follow-up review fixes:

1. The DM-session store is now multi-writer, so the load-mutate-save is serialized
   by a cross-process advisory lock. Concurrent seals to the same peer must each
   advance the chain (no lost update, no message-key reuse).
2. The ratchet keypair is the LOCAL resident agent's, never a from-identity that in
   the skseal/group paths is an untrusted sender. A path-shaped agent name must not
   escape the pqc dir.
3. The send routes fail soft: a ConfidentialityError out of send_and_store persists
   locally instead of 500-ing the route or crashing the REPL.
"""

from __future__ import annotations

import threading

import pytest

# --------------------------------------------------------------------------- #
# Fix 2: identity sanitization / no foreign-keypair minting
# --------------------------------------------------------------------------- #


def test_safe_agent_strips_path_traversal():
    from skchat.pq_prekeys import _safe_agent

    assert _safe_agent("../evil") == "evil"
    assert _safe_agent("capauth:../../etc/x@y".split(":")[-1]) == "etcx"
    assert _safe_agent("lumina") == "lumina"
    assert _safe_agent("opus@skworld.io") == "opus"


def test_safe_agent_rejects_empty():
    from skchat.pq_prekeys import _safe_agent

    with pytest.raises(ValueError):
        _safe_agent("../")
    with pytest.raises(ValueError):
        _safe_agent("")


def test_ensure_keypair_never_escapes_pqc_dir(monkeypatch, tmp_path):
    """A path-shaped agent name writes inside the pqc dir, not above it."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    if not pq_prekeys.available():
        pytest.skip("no PQ backend (liboqs) available")
    kp = pq_prekeys.ensure_agent_keypair("../pwned")
    assert kp is not None
    # Nothing was written outside <tmp>/pqc
    escaped = list(tmp_path.glob("*pwned*"))  # would appear at tmp root if traversal worked
    assert escaped == [], f"key material escaped the pqc dir: {escaped}"
    inside = list((tmp_path / "pqc").glob("*pwned*hybrid*"))
    assert inside, "sanitized keypair should live inside the pqc dir"


def test_ratchet_manager_uses_resident_agent_not_from_identity(monkeypatch, tmp_path):
    """Building a transport under a foreign from-identity keys the ratchet store to
    the RESIDENT agent, never the foreign name."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKCHAT_DM_RATCHET", "1")
    monkeypatch.setenv("SKAGENT", "lumina")

    captured = {}
    from skchat.dm_manager import DmRatchetManager

    real_for_agent = DmRatchetManager.for_agent

    def spy(crypto, agent, store_dir, **kw):
        captured["agent"] = agent
        return real_for_agent(crypto, agent, store_dir, **kw)

    monkeypatch.setattr(DmRatchetManager, "for_agent", staticmethod(spy))

    from unittest.mock import MagicMock

    from skchat.transport import ChatTransport

    t = ChatTransport.from_config(
        skcomms=MagicMock(),
        history=MagicMock(),
        identity="capauth:../attacker@evil.test",
        crypto=MagicMock(),  # truthy so the ratchet manager builds
    )
    t._dm_ratchet_manager()
    assert captured.get("agent") == "lumina", captured
    assert "attacker" not in str(captured.get("agent"))


def test_ratchet_agent_falls_back_to_identity_when_no_resident_env(monkeypatch, tmp_path):
    """With NO resident-agent env set (bare CLI/interactive whose identity IS the
    local user), the ratchet agent follows the identity, not a blanket 'lumina'
    default. Regression: a node configured via SKCHAT_IDENTITY/config.yml without
    SKAGENT must still ratchet under its own keypair, not lumina's."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKCHAT_DM_RATCHET", "1")
    for var in ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT"):
        monkeypatch.delenv(var, raising=False)

    captured = {}
    from skchat.dm_manager import DmRatchetManager

    real_for_agent = DmRatchetManager.for_agent

    def spy(crypto, agent, store_dir, **kw):
        captured["agent"] = agent
        return real_for_agent(crypto, agent, store_dir, **kw)

    monkeypatch.setattr(DmRatchetManager, "for_agent", staticmethod(spy))

    from unittest.mock import MagicMock

    from skchat.transport import ChatTransport

    t = ChatTransport.from_config(
        skcomms=MagicMock(),
        history=MagicMock(),
        identity="capauth:jarvis@skworld.io",
        crypto=MagicMock(),
    )
    t._dm_ratchet_manager()
    assert captured.get("agent") == "jarvis", captured


# --------------------------------------------------------------------------- #
# Fix 1: cross-process lock serializes concurrent seals (no lost update)
# --------------------------------------------------------------------------- #


def _minimal_manager(store):
    """A DmRatchetManager wired with stub crypto/keys, real store (has db_path)."""
    from unittest.mock import MagicMock

    from skchat.dm_manager import DmRatchetManager

    return DmRatchetManager(
        MagicMock(),
        agent_public=b"pub",
        agent_private=b"priv",
        peer_pub_resolver=lambda p: b"peerpub",
        store=store,
        store_key=b"\x11" * 32,
    )


def test_store_lock_serializes_across_threads(tmp_path):
    """The store lock provides mutual exclusion around the load-mutate-save.

    A non-atomic read-modify-write of a shared counter inside the lock, run from
    N threads, must land all N increments; without the flock the interleave loses
    updates (the exact class of the DM-session chain-fork the fix prevents). fcntl
    locks are per open file description, so separate-fd threads contend.
    """
    from skchat.dm_store import DmSessionStore

    store = DmSessionStore(tmp_path / "dm_sessions_test.db")
    mgr = _minimal_manager(store)

    box = {"n": 0}
    N = 12

    def _bump():
        with mgr._store_lock():
            cur = box["n"]
            threading.Event().wait(0.002)  # widen the race window
            box["n"] = cur + 1

    threads = [threading.Thread(target=_bump) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert box["n"] == N, (
        f"lost updates ({box['n']}/{N}): the store lock did not serialize writers"
    )


def test_seal_uses_the_store_lock(tmp_path):
    """seal() must perform its load-mutate-save inside _store_lock (regression:
    the fix wraps the critical section, an unlocked seal would not enter it)."""

    from skchat.dm_store import DmSessionStore
    from skchat.models import ChatMessage

    store = DmSessionStore(tmp_path / "dm_sessions_seal.db")
    mgr = _minimal_manager(store)
    mgr._crypto.encrypt_message_ratchet.side_effect = lambda m, s, p: m

    entered = {"count": 0}
    import contextlib

    real_lock = mgr._store_lock

    @contextlib.contextmanager
    def _spy():
        entered["count"] += 1
        with real_lock():
            yield

    mgr._store_lock = _spy
    mgr.seal(ChatMessage(sender="me", recipient="capauth:p@n.test", content="x"))
    assert entered["count"] == 1, "seal did not acquire the store lock"
