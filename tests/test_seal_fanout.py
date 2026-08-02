"""Task 9: sender fanout seal in ``_seal_hybrid_outbound``.

When the peer's published bundle set advertises ``codec: "pqdm2"``, the sealer
fans the reply out to EVERY peer device slot AND every sender-own device slot
(one message key, wrapped per slot) as a ``pqdm2:`` token. A peer that never
advertised ``pqdm2`` still gets the classical single-slot ``pqdm1:`` envelope.
Partial sealability seals to the sealable set and logs the skipped ``key_id``s;
a fully unsealable hybrid channel still fails closed (returns ``None``).
"""

import base64
import json

from skcomms import pqdm

from skchat import daemon_proxy, pq_prekeys


def _device():
    """A fresh hybrid keypair -> (public_hex, private_hex)."""
    return pqdm.generate_hybrid_keypair()


def _slot(public_hex: str, *, codec: str | None = None, ts: int = 1) -> dict:
    slot = {
        "suite": pqdm.HYBRID_SUITE,
        "hybrid_public_hex": public_hex,
        "key_id": public_hex[:16],
        "last_published": ts,
    }
    if codec is not None:
        slot["codec"] = codec
    return slot


def _pqdm2_kids(token: str) -> set[str]:
    header_b64 = token[len("pqdm2:") :].split(".", 1)[0]
    header = json.loads(base64.b64decode(header_b64))
    return set(header["kids"])


def test_pqdm2_fanout_to_every_peer_and_own_slot(monkeypatch):
    peer_a, _ = _device()
    peer_b, _ = _device()
    own, _ = _device()

    peer_slots = [
        _slot(peer_a, codec="pqdm2", ts=2),
        _slot(peer_b, codec="pqdm2", ts=1),
    ]
    own_slots = [_slot(own, codec="pqdm2", ts=1)]

    def fake_load(short: str):
        return own_slots if short == "lumina" else peer_slots

    monkeypatch.setattr(pq_prekeys, "load_peer_bundles", fake_load)

    token = daemon_proxy._seal_hybrid_outbound("hello", recipient_short="chef")

    assert token is not None
    assert token.startswith("pqdm2:")
    assert _pqdm2_kids(token) == {peer_a[:16], peer_b[:16], own[:16]}


def test_pqdm2_partial_seals_sealable_and_skips_bad_slot(monkeypatch, caplog):
    good, _ = _device()
    bad = _slot("nothex", codec="pqdm2", ts=1)  # malformed hybrid key -> skipped
    peer_slots = [_slot(good, codec="pqdm2", ts=2), bad]

    def fake_load(short: str):
        return [] if short == "lumina" else peer_slots

    monkeypatch.setattr(pq_prekeys, "load_peer_bundles", fake_load)

    import logging

    with caplog.at_level(logging.WARNING):
        token = daemon_proxy._seal_hybrid_outbound("hi", recipient_short="chef")

    assert token is not None
    assert token.startswith("pqdm2:")
    # Only the sealable device is a slot; the malformed key_id is logged.
    assert _pqdm2_kids(token) == {good[:16]}
    assert bad["key_id"] in caplog.text


def test_pqdm2_fails_closed_when_no_slot_sealable(monkeypatch):
    peer_slots = [_slot("nothex", codec="pqdm2", ts=1)]  # advertises pqdm2 but unusable

    def fake_load(short: str):
        return [] if short == "lumina" else peer_slots

    monkeypatch.setattr(pq_prekeys, "load_peer_bundles", fake_load)

    token = daemon_proxy._seal_hybrid_outbound("hi", recipient_short="chef")

    # Fail closed: caller then refuses to leak plaintext (reply_not_sealable).
    assert token is None


def test_pqdm1_fallback_when_peer_has_no_codec_advert(monkeypatch):
    peer_a, _ = _device()
    peer_slots = [_slot(peer_a, ts=1)]  # no codec advert

    def fake_load(short: str):
        return peer_slots if short == "chef" else []

    monkeypatch.setattr(pq_prekeys, "load_peer_bundles", fake_load)

    token = daemon_proxy._seal_hybrid_outbound("hi", recipient_short="chef")

    assert token is not None
    assert token.startswith("pqdm1:")
