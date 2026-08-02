"""Best-effort TTL re-pull of a stale cross-node peer prekey (coord 4c054eab).

``_seal_hybrid_outbound`` loads the peer's device slots from the LOCAL prekey
store and never re-pulls, so a CROSS-NODE peer's cached bundle can go stale:
after that peer republishes a fresh/new device slot, Lumina keeps sealing to the
stale local copy until something re-pulls. This adds a best-effort TTL refresh
(``_maybe_refresh_peer``) that fires just before the seal loads slots.

Contract exercised here:

* A stale local bundle (newest ``last_published`` older than the TTL) triggers a
  ``prekey_exchange.fetch_peer_prekey`` re-pull before sealing.
* A fresh local bundle (within the TTL) does NOT re-pull.
* A re-pull that raises is swallowed (best-effort): sealing still proceeds with
  whatever local slots exist and NEVER raises.
* No local slots + a re-pull that brings some => those fresh slots get used.

Same-daemon peers (e.g. ``chef``) POST into the store directly and are already
fresh; this is the cross-node case. The re-pull is best-effort and must NEVER
raise or block a send.
"""

import time

from skcomms import pqdm

from skchat import daemon_proxy, pq_prekeys, prekey_exchange

TTL = daemon_proxy._DEFAULT_PREKEY_REFRESH_TTL


def _device():
    """A fresh hybrid keypair -> (public_hex, private_hex)."""
    return pqdm.generate_hybrid_keypair()


def _slot(public_hex: str, *, ts: float) -> dict:
    """A single classical-fallback (no codec) device slot at published time *ts*."""
    return {
        "suite": pqdm.HYBRID_SUITE,
        "hybrid_public_hex": public_hex,
        "key_id": public_hex[:16],
        "last_published": ts,
    }


def _reset_throttle():
    """Clear the in-process re-pull throttle so each test starts clean."""
    daemon_proxy._last_refresh_attempt.clear()


def test_stale_bundle_triggers_repull_before_sealing(monkeypatch):
    _reset_throttle()
    peer, _ = _device()
    stale_ts = time.time() - (TTL + 3600)  # older than the TTL -> stale
    peer_slots = [_slot(peer, ts=stale_ts)]

    calls: list[str] = []

    def fake_fetch(peer_fqid, **kwargs):
        calls.append(peer_fqid)
        return None  # store already holds the (stale) slot; nothing new here

    def fake_load(short: str):
        return peer_slots if short == "remotepeer_stale" else []

    monkeypatch.setattr(prekey_exchange, "fetch_peer_prekey", fake_fetch)
    monkeypatch.setattr(pq_prekeys, "load_peer_bundles", fake_load)

    token = daemon_proxy._seal_hybrid_outbound("hi", recipient_short="remotepeer_stale")

    assert calls, "a stale bundle must trigger a re-pull before sealing"
    # Sealing still proceeds with the local slot.
    assert token is not None
    assert token.startswith("pqdm1:")


def test_fresh_bundle_does_not_repull(monkeypatch):
    _reset_throttle()
    peer, _ = _device()
    fresh_ts = time.time()  # within the TTL -> fresh
    peer_slots = [_slot(peer, ts=fresh_ts)]

    calls: list[str] = []

    def fake_fetch(peer_fqid, **kwargs):
        calls.append(peer_fqid)
        return None

    def fake_load(short: str):
        return peer_slots if short == "remotepeer_fresh" else []

    monkeypatch.setattr(prekey_exchange, "fetch_peer_prekey", fake_fetch)
    monkeypatch.setattr(pq_prekeys, "load_peer_bundles", fake_load)

    token = daemon_proxy._seal_hybrid_outbound("hi", recipient_short="remotepeer_fresh")

    assert not calls, "a fresh bundle within the TTL must NOT re-pull"
    assert token is not None
    assert token.startswith("pqdm1:")


def test_repull_that_raises_is_swallowed_and_seal_proceeds(monkeypatch):
    _reset_throttle()
    peer, _ = _device()
    stale_ts = time.time() - (TTL + 3600)
    peer_slots = [_slot(peer, ts=stale_ts)]

    def boom(peer_fqid, **kwargs):
        raise RuntimeError("network exploded")

    def fake_load(short: str):
        return peer_slots if short == "remotepeer_boom" else []

    monkeypatch.setattr(prekey_exchange, "fetch_peer_prekey", boom)
    monkeypatch.setattr(pq_prekeys, "load_peer_bundles", fake_load)

    # Best-effort: the raising re-pull must NOT propagate; the seal still runs on
    # the local slot.
    token = daemon_proxy._seal_hybrid_outbound("hi", recipient_short="remotepeer_boom")

    assert token is not None
    assert token.startswith("pqdm1:")


def test_no_local_slots_then_repull_brings_some(monkeypatch):
    _reset_throttle()
    peer, _ = _device()
    state = {"pulled": False}

    def fake_fetch(peer_fqid, **kwargs):
        # Models fetch_peer_prekey persisting a fresh slot via store_peer_bundle:
        # after it runs, load_peer_bundles returns the new slot.
        state["pulled"] = True
        return _slot(peer, ts=time.time())

    def fake_load(short: str):
        if short == "remotepeer_empty" and state["pulled"]:
            return [_slot(peer, ts=time.time())]
        return []  # no local slots (peer), and none for the sender "lumina"

    monkeypatch.setattr(prekey_exchange, "fetch_peer_prekey", fake_fetch)
    monkeypatch.setattr(pq_prekeys, "load_peer_bundles", fake_load)

    token = daemon_proxy._seal_hybrid_outbound("hi", recipient_short="remotepeer_empty")

    assert state["pulled"], "no local slots must trigger a re-pull"
    # The freshly pulled slot is used to seal.
    assert token is not None
    assert token.startswith("pqdm1:")
