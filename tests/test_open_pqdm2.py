"""PQC multi-device fanout (Phase 1), Task 10 - receiver opens pqdm2 (server).

``_open_hybrid_inbound`` learns the multi-recipient ``pqdm2:`` envelope: it picks
the daemon's OWN device slot (its published ``key_id``) out of the fanout token
and opens it via ``skcomms.pqdm.open_multi``. A token that carries no slot for
this device returns ``None`` (the graceful locked placeholder path), NEVER a
crash. The legacy single-recipient ``pqdm1:`` path is unchanged.
"""

import base64
import importlib

import pytest
from skcomms import pqdm


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh pq_prekeys + daemon_proxy bound to an isolated SKCHAT_HOME."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    from skchat import daemon_proxy

    return daemon_proxy, pq_prekeys


def _own_slot(PQ):
    """Lumina's OWN hybrid device slot: (key_id, public_hex)."""
    kp = PQ.ensure_lumina_keypair()
    assert kp is not None, "lumina hybrid keypair (liboqs) required for this test"
    pub_hex = kp[0].hex()
    return pub_hex[:16], pub_hex


def test_open_pqdm2_picks_own_slot(env):
    daemon_proxy, PQ = env
    own_kid, own_pub = _own_slot(PQ)
    peer_pub, _ = pqdm.generate_hybrid_keypair()
    recips = [
        {"key_id": own_kid, "hybrid_public_hex": own_pub},
        {"key_id": peer_pub[:16], "hybrid_public_hex": peer_pub},
    ]
    tok = pqdm.seal_multi(b"hello from chef", recips, sender="chef", recipient_id="lumina")
    assert tok.startswith("pqdm2:")

    out = daemon_proxy._open_hybrid_inbound(tok, sender_short="chef")
    assert out == "hello from chef"


def test_open_pqdm2_missing_own_slot_returns_none(env):
    daemon_proxy, PQ = env
    _own_slot(PQ)  # lumina has a keypair, but it is NOT in the token
    a_pub, _ = pqdm.generate_hybrid_keypair()
    b_pub, _ = pqdm.generate_hybrid_keypair()
    recips = [
        {"key_id": a_pub[:16], "hybrid_public_hex": a_pub},
        {"key_id": b_pub[:16], "hybrid_public_hex": b_pub},
    ]
    tok = pqdm.seal_multi(b"secret", recips, sender="chef", recipient_id="lumina")

    # No slot for this device -> graceful None (locked placeholder), not a crash.
    assert daemon_proxy._open_hybrid_inbound(tok, sender_short="chef") is None


def test_open_pqdm2_garbage_token_returns_none(env):
    daemon_proxy, PQ = env
    _own_slot(PQ)
    assert daemon_proxy._open_hybrid_inbound("pqdm2:not-a-real-token", sender_short="chef") is None


def test_pqdm1_path_unchanged(env):
    daemon_proxy, PQ = env
    kp = PQ.ensure_lumina_keypair()
    bundle = pqdm.PrekeyBundle(suite=pqdm.HYBRID_SUITE, hybrid_public_hex=kp[0].hex())
    sealed = pqdm.seal(b"legacy hi", bundle, sender="chef", recipient="lumina")
    tok = f"pqdm1:{pqdm.HYBRID_SUITE}:" + base64.b64encode(sealed).decode("ascii")

    assert daemon_proxy._open_hybrid_inbound(tok, sender_short="chef") == "legacy hi"
