"""Tests for the SealedDmFrame wire codec (RFC-0001 P1 — message-path usability).

The codec gives :class:`skchat.dm_session.SealedDmFrame` a compact, explicit
binary serialization so a sealed ratchet frame can live in
``ChatMessage.content`` (mirroring the ``pqdm1:`` token shape that the hybrid
DM path already uses). Wire form::

    pqdr1: + base64(  epoch(u64) || index(u64)
                    || nonce_len(u32) || nonce
                    || body_len(u32)  || body
                    || kam_flag(u8)   || [kam_len(u32) || kam] )

Round-trip MUST be exact — including the ``kam=None`` vs ``kam=present`` distinction.
"""

from __future__ import annotations

import base64

import pytest

from skchat.dm_session import PQDR_SCHEME, SealedDmFrame


def test_roundtrip_with_kam():
    frame = SealedDmFrame(
        epoch=3,
        index=7,
        nonce=b"\x01" * 12,
        body=b"sealed-ciphertext-and-tag",
        kam=b"wrapped-epoch-secret-bytes",
    )
    token = frame.to_token()
    assert token.startswith(PQDR_SCHEME)
    out = SealedDmFrame.from_token(token)
    assert out.epoch == frame.epoch
    assert out.index == frame.index
    assert out.nonce == frame.nonce
    assert out.body == frame.body
    assert out.kam == frame.kam


def test_roundtrip_without_kam():
    frame = SealedDmFrame(
        epoch=0,
        index=0,
        nonce=b"\xab" * 12,
        body=b"body-only-no-kam",
        kam=None,
    )
    token = frame.to_token()
    out = SealedDmFrame.from_token(token)
    assert out.kam is None
    assert (out.epoch, out.index, out.nonce, out.body) == (0, 0, b"\xab" * 12, b"body-only-no-kam")


def test_kam_none_vs_empty_bytes_distinct():
    """kam=None and kam=b'' must NOT collapse to the same wire form."""
    none_token = SealedDmFrame(epoch=1, index=1, nonce=b"n" * 12, body=b"x", kam=None).to_token()
    empty_token = SealedDmFrame(epoch=1, index=1, nonce=b"n" * 12, body=b"x", kam=b"").to_token()
    assert none_token != empty_token
    assert SealedDmFrame.from_token(none_token).kam is None
    assert SealedDmFrame.from_token(empty_token).kam == b""


def test_rejects_non_pqdr1_token():
    with pytest.raises(ValueError):
        SealedDmFrame.from_token("pqdm1:x25519-mlkem768:" + base64.b64encode(b"abc").decode())
    with pytest.raises(ValueError):
        SealedDmFrame.from_token("not-a-token")


def test_rejects_truncated_and_garbage():
    good = SealedDmFrame(epoch=2, index=2, nonce=b"n" * 12, body=b"body", kam=None).to_token()
    raw = base64.b64decode(good[len(PQDR_SCHEME) :])
    truncated = PQDR_SCHEME + base64.b64encode(raw[:8]).decode("ascii")
    with pytest.raises(ValueError):
        SealedDmFrame.from_token(truncated)
    with pytest.raises(ValueError):
        SealedDmFrame.from_token(PQDR_SCHEME + "!!!not-base64!!!")
    with pytest.raises(ValueError):
        SealedDmFrame.from_token(PQDR_SCHEME + base64.b64encode(b"\x00" * 4).decode("ascii"))
