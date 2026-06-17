"""Tests for GlossaMeshGatekeeper (U9) — capauth source-auth for mesh frames.

The capauth sign/verify backends are mocked with an in-memory fake keyring so no
real keys/network are touched. The fake mirrors the real capauth backend shape:
``sign(data) -> sig`` and ``verify(data, sig) -> authenticated_fqid | None``.
"""

from __future__ import annotations

import base64
import json

import pytest

# skcomms is an optional dep pulled in transitively by skchat.glossa_mesh.protocol;
# the gatekeeper itself has no skcomms dep, but we exercise it against real frames.
pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

from skchat.glossa_mesh import protocol
from skchat.glossa_mesh.gatekeeper import (
    GlossaMeshGatekeeper,
    MalformedEnvelopeError,
    MissingSignatureError,
    SignatureVerificationError,
    SourceSpoofError,
)


class FakeKeyring:
    """In-memory stand-in for the capauth crypto backend.

    A signature is the literal ``"<fqid>:<sha-ish>"``; verify recomputes it and
    returns the embedded FQID iff it matches the data. No crypto, no keys —
    enough to prove the gatekeeper's source-binding and tamper logic.
    """

    def __init__(self, fqid: str) -> None:
        self.fqid = fqid

    @staticmethod
    def _tag(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    def signer(self, data: bytes) -> str:
        return f"{self.fqid}|{self._tag(data)}"

    @staticmethod
    def verifier(data: bytes, sig: str) -> str | None:
        """Return the FQID the signature authenticates to, or None on mismatch."""
        try:
            fqid, tag = sig.split("|", 1)
        except ValueError:
            return None
        if tag != FakeKeyring._tag(data):
            return None  # data was tampered after signing
        return fqid


def _gatekeeper(fqid: str) -> tuple[GlossaMeshGatekeeper, FakeKeyring]:
    kr = FakeKeyring(fqid)
    gk = GlossaMeshGatekeeper(source_fqid=fqid, signer=kr.signer, verifier=kr.verifier)
    return gk, kr


def _frame() -> bytes:
    return protocol.frame_message(2, b"hello-mesh")


# --- 1. round-trip ---------------------------------------------------------


def test_sign_verify_round_trip():
    gk, _ = _gatekeeper("alice@x.y")
    frame = _frame()
    signed = gk.wrap_outbound(frame)
    source, out = gk.unwrap_inbound(signed)
    assert source == "alice@x.y"
    assert out == frame


def test_round_trip_across_two_nodes_shared_verifier():
    # alice signs; bob (different source identity) verifies with the same scheme.
    alice, _ = _gatekeeper("alice@x.y")
    signed = alice.wrap_outbound(_frame())
    bob = GlossaMeshGatekeeper(
        source_fqid="bob@x.y", signer=FakeKeyring("bob@x.y").signer, verifier=FakeKeyring.verifier
    )
    source, out = bob.unwrap_inbound(signed)
    assert source == "alice@x.y"
    assert out == _frame()


def test_wrap_outbound_is_self_describing_envelope():
    gk, _ = _gatekeeper("alice@x.y")
    env = json.loads(gk.wrap_outbound(_frame()).decode())
    assert env["source"] == "alice@x.y"
    assert base64.b64decode(env["frame"]) == _frame()
    assert env["sig"]


# --- 2. tampering ----------------------------------------------------------


def test_tampered_frame_body_fails_verification():
    gk, _ = _gatekeeper("alice@x.y")
    env = json.loads(gk.wrap_outbound(_frame()).decode())
    env["frame"] = base64.b64encode(protocol.frame_message(2, b"EVIL")).decode("ascii")
    tampered = json.dumps(env).encode()
    with pytest.raises(SignatureVerificationError):
        gk.unwrap_inbound(tampered)


def test_tampered_signature_fails_verification():
    gk, _ = _gatekeeper("alice@x.y")
    env = json.loads(gk.wrap_outbound(_frame()).decode())
    env["sig"] = "alice@x.y|" + base64.b64encode(b"garbage").decode("ascii")
    with pytest.raises(SignatureVerificationError):
        gk.unwrap_inbound(json.dumps(env).encode())


# --- 3. wrong-source-FQID (the anti-spoof core) ----------------------------


def test_wrong_source_fqid_is_rejected():
    # mallory signs a frame correctly but rewrites the claimed source to alice.
    # Because the source is bound into the signed bytes, simply swapping the
    # source field breaks the signature...
    mallory, _ = _gatekeeper("mallory@x.y")
    env = json.loads(mallory.wrap_outbound(_frame()).decode())
    env["source"] = "alice@x.y"
    with pytest.raises(SignatureVerificationError):
        mallory.unwrap_inbound(json.dumps(env).encode())


def test_source_spoof_when_signer_authenticates_to_other_fqid():
    # ...and even a verifier that authenticates to a DIFFERENT fqid than the
    # claimed source is rejected by the explicit anti-spoof check. Here the
    # verifier always reports "mallory@x.y" regardless of the envelope source.
    def lying_signer(data: bytes) -> str:
        return "sig-for-mallory"

    def verifier_returns_mallory(data: bytes, sig: str) -> str:
        return "mallory@x.y"  # authenticated identity != claimed source

    gk = GlossaMeshGatekeeper(
        source_fqid="alice@x.y", signer=lying_signer, verifier=verifier_returns_mallory
    )
    signed = gk.wrap_outbound(_frame())  # stamps source=alice@x.y
    with pytest.raises(SourceSpoofError):
        gk.unwrap_inbound(signed)


# --- 4. missing / malformed signature handling -----------------------------


def test_missing_signature_is_rejected():
    gk, _ = _gatekeeper("alice@x.y")
    env = json.loads(gk.wrap_outbound(_frame()).decode())
    env["sig"] = ""
    with pytest.raises(MissingSignatureError):
        gk.unwrap_inbound(json.dumps(env).encode())


def test_absent_signature_field_is_rejected():
    gk, _ = _gatekeeper("alice@x.y")
    env = json.loads(gk.wrap_outbound(_frame()).decode())
    env.pop("sig")
    with pytest.raises(MissingSignatureError):
        gk.unwrap_inbound(json.dumps(env).encode())


def test_non_json_envelope_is_malformed():
    gk, _ = _gatekeeper("alice@x.y")
    with pytest.raises(MalformedEnvelopeError):
        gk.unwrap_inbound(b"\x00\x01not-json")


def test_missing_source_field_is_malformed():
    gk, _ = _gatekeeper("alice@x.y")
    bad = json.dumps({"frame": base64.b64encode(_frame()).decode(), "sig": "x|y"}).encode()
    with pytest.raises(MalformedEnvelopeError):
        gk.unwrap_inbound(bad)


def test_bad_base64_frame_is_malformed():
    gk, _ = _gatekeeper("alice@x.y")
    bad = json.dumps({"source": "alice@x.y", "frame": "!!!not-b64!!!", "sig": "x|y"}).encode()
    with pytest.raises(MalformedEnvelopeError):
        gk.unwrap_inbound(bad)


def test_verifier_exception_becomes_verification_error():
    def boom_verifier(data: bytes, sig: str) -> str:
        raise RuntimeError("backend exploded")

    gk = GlossaMeshGatekeeper(
        source_fqid="alice@x.y", signer=FakeKeyring("alice@x.y").signer, verifier=boom_verifier
    )
    signed = gk.wrap_outbound(_frame())
    with pytest.raises(SignatureVerificationError):
        gk.unwrap_inbound(signed)


# --- 5. round-trips over real announce frames too --------------------------


def test_round_trip_over_announce_frame():
    from skcomms.glossa.codebook import default_codebook
    from skcomms.glossa.handshake import CapabilityDescriptor

    desc = CapabilityDescriptor(
        fqid="alice@x.y",
        model_tier="large",
        max_level=2,
        codebook_version=default_codebook().version,
        lexicon_version="",
    )
    gk, _ = _gatekeeper("alice@x.y")
    frame = protocol.frame_announce(desc)
    source, out = gk.unwrap_inbound(gk.wrap_outbound(frame))
    assert source == "alice@x.y"
    assert protocol.read_announce(protocol.parse_frame(out)[1]) == desc
