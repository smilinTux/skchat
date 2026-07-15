"""Tests for the Mode-C gift-wrap envelope (NIP-59 style, PQ-sealed).

Covers ``docs/2026-07-15-sovereign-invite-join-architecture.md`` §4 step 2 +
hardening H6: a per-recipient sealed envelope that privately delivers a Mode-C
invite/accept payload over a dumb, zero-trust rendezvous relay.

The security contract under test:

* ``seal_giftwrap`` → ``open_giftwrap`` round-trips the inner payload with the
  **real** hybrid PQ KEM (x25519 + ML-KEM-768, reused from ``atrest_wrap``).
* the OUTER envelope leaks nothing that identifies the sender: no sender
  identity, no true timestamp, no inner ``kind`` — only a throwaway public key
  and the recipient fingerprint tag.
* a wrong recipient key cannot open; a tampered ciphertext fails closed
  (raises, never a partial/plaintext leak).

These require the liboqs (``oqs``) backend; skipped cleanly if unavailable
(mirrors ``test_atrest_wrap.py``).
"""

from __future__ import annotations

import importlib
import json

import pytest

pqkem = importlib.import_module("skcomms.pqkem")

pytestmark = pytest.mark.skipif(
    not pqkem.is_available(),
    reason="liboqs/oqs backend unavailable — hybrid KEM cannot run",
)

# Imported after the skip-marker on purpose (mirrors test_atrest_wrap.py): the
# module is import-safe but the KEM ops it wraps are oqs-gated.
atrest_wrap = importlib.import_module("skchat.atrest_wrap")
giftwrap = importlib.import_module("skchat.guest_giftwrap")


# A distinctive inner payload: markers that MUST NOT appear anywhere in the
# outer envelope (they live only inside the sealed ciphertext).
def _inner():
    return {
        "kind": "MODE_C_INVITE_KIND_MARKER",
        "sender": "capauth:opus@skworld.io",
        "sender_fqid": "opus@skworld.smilintux.SENDER_IDENTITY_MARKER",
        "true_ts": 1_700_000_123,
        "invite_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.MARKER.sig",
        "k": "FRAGMENT_SECRET_MARKER",
    }


@pytest.fixture()
def _enabled(monkeypatch):
    monkeypatch.setenv("SKCHAT_PQ_INVITES_ENABLED", "1")


@pytest.fixture()
def recipient():
    kp = atrest_wrap.new_recipient_keypair()
    return kp


# ---------------------------------------------------------------------------
# Round-trip with the real PQ KEM
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_seal_open_roundtrips_inner(self, _enabled, recipient):
        inner = _inner()
        env = giftwrap.seal_giftwrap(inner, recipient.public_key.hex())
        recovered = giftwrap.open_giftwrap(env, recipient.private_key.hex())
        assert recovered == inner

    def test_envelope_is_a_plain_dict(self, _enabled, recipient):
        env = giftwrap.seal_giftwrap(_inner(), recipient.public_key.hex())
        assert isinstance(env, dict)
        # JSON-serializable so it can ride skcomms / a Nostr relay verbatim.
        json.dumps(env)

    def test_each_seal_unique(self, _enabled, recipient):
        pub = recipient.public_key.hex()
        e1 = giftwrap.seal_giftwrap(_inner(), pub)
        e2 = giftwrap.seal_giftwrap(_inner(), pub)
        assert e1["ciphertext"] != e2["ciphertext"]
        # Fresh throwaway key each time (unlinkable across sends).
        assert e1["throwaway_pub"] != e2["throwaway_pub"]
        assert (
            giftwrap.open_giftwrap(e1, recipient.private_key.hex())
            == giftwrap.open_giftwrap(e2, recipient.private_key.hex())
        )


# ---------------------------------------------------------------------------
# Metadata privacy — the outer reveals nothing about the sender
# ---------------------------------------------------------------------------


class TestOuterRevealsNothing:
    def test_outer_hides_sender_kind_and_true_ts(self, _enabled, recipient):
        inner = _inner()
        env = giftwrap.seal_giftwrap(inner, recipient.public_key.hex())

        # Serialize the WHOLE envelope: because the inner is sealed, none of the
        # sensitive plaintext markers may appear anywhere (not in metadata, not
        # in the ciphertext which is opaque base64).
        blob = json.dumps(env)
        assert "SENDER_IDENTITY_MARKER" not in blob
        assert inner["sender"] not in blob
        assert inner["kind"] not in blob
        assert "MODE_C_INVITE_KIND_MARKER" not in blob
        assert "FRAGMENT_SECRET_MARKER" not in blob

        # The outer carries ONLY a throwaway key + recipient tag + randomized ts.
        assert env["throwaway_pub"]
        assert env["p_tag"] == giftwrap.recipient_fingerprint(recipient.public_key.hex())
        # No sender/idm/kind fields on the outer at all.
        assert "sender" not in env
        assert "idm" not in env
        assert "kind" not in env
        # The true timestamp is not exposed as any outer value.
        assert inner["true_ts"] not in env.values()

    def test_created_at_is_randomized_not_the_true_ts(self, _enabled, recipient):
        inner = _inner()
        e1 = giftwrap.seal_giftwrap(inner, recipient.public_key.hex())
        e2 = giftwrap.seal_giftwrap(inner, recipient.public_key.hex())
        assert e1["created_at"] != inner["true_ts"]
        assert e2["created_at"] != inner["true_ts"]
        # Randomized across sends (does not commit to a real, correlatable clock).
        assert e1["created_at"] != e2["created_at"]

    def test_p_tag_is_a_fingerprint_not_the_pubkey(self, _enabled, recipient):
        pub = recipient.public_key.hex()
        env = giftwrap.seal_giftwrap(_inner(), pub)
        # The tag is a short fingerprint, never the full recipient public key.
        assert env["p_tag"] != pub
        assert pub not in json.dumps(env)
        assert len(env["p_tag"]) < len(pub)


# ---------------------------------------------------------------------------
# Fail-closed: wrong key, tamper, disabled flag
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_wrong_recipient_key_cannot_open(self, _enabled, recipient):
        other = atrest_wrap.new_recipient_keypair()
        env = giftwrap.seal_giftwrap(_inner(), recipient.public_key.hex())
        with pytest.raises(giftwrap.GiftwrapError):
            giftwrap.open_giftwrap(env, other.private_key.hex())

    def test_tampered_ciphertext_fails_closed(self, _enabled, recipient):
        env = giftwrap.seal_giftwrap(_inner(), recipient.public_key.hex())
        ct = bytearray(giftwrap._b64d(env["ciphertext"]))
        ct[-1] ^= 0x01  # flip one bit of the sealed body
        env["ciphertext"] = giftwrap._b64e(bytes(ct))
        with pytest.raises(giftwrap.GiftwrapError):
            giftwrap.open_giftwrap(env, recipient.private_key.hex())

    def test_tampered_outer_metadata_fails_closed(self, _enabled, recipient):
        # p_tag / created_at are bound into the seal's AAD → mutating them breaks
        # the inner authentication (fail-closed), not just the outer signature.
        env = giftwrap.seal_giftwrap(_inner(), recipient.public_key.hex())
        env["created_at"] = env["created_at"] + 99999
        with pytest.raises(giftwrap.GiftwrapError):
            giftwrap.open_giftwrap(env, recipient.private_key.hex())

    def test_tampered_outer_signature_fails_closed(self, _enabled, recipient):
        env = giftwrap.seal_giftwrap(_inner(), recipient.public_key.hex())
        env["sig"] = giftwrap._b64e(b"\x00" * 64)
        with pytest.raises(giftwrap.GiftwrapError):
            giftwrap.open_giftwrap(env, recipient.private_key.hex())

    def test_seal_requires_flag(self, monkeypatch, recipient):
        monkeypatch.delenv("SKCHAT_PQ_INVITES_ENABLED", raising=False)
        with pytest.raises(giftwrap.GiftwrapError):
            giftwrap.seal_giftwrap(_inner(), recipient.public_key.hex())

    def test_open_requires_flag(self, monkeypatch, recipient):
        monkeypatch.setenv("SKCHAT_PQ_INVITES_ENABLED", "1")
        env = giftwrap.seal_giftwrap(_inner(), recipient.public_key.hex())
        monkeypatch.delenv("SKCHAT_PQ_INVITES_ENABLED", raising=False)
        with pytest.raises(giftwrap.GiftwrapError):
            giftwrap.open_giftwrap(env, recipient.private_key.hex())
