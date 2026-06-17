"""GlossaMeshGatekeeper (spec U9) — capauth source-authentication for mesh frames.

The mesh ``MeshBus`` is a reliable broadcast medium: any started member can put
bytes on the wire claiming to be any source. The gatekeeper closes that hole by
signing every outbound frame with the *local* capauth identity and verifying
every inbound frame against the source FQID it claims.

Wire shape (a thin self-describing envelope around the raw mesh frame produced by
``skchat.glossa_mesh.protocol``)::

    {"source": "<signer-fqid>", "frame": "<base64(frame_bytes)>", "sig": "<armor>"}

The signed payload is the canonical (source, frame) tuple, so a frame cannot be
replayed under a different source nor have its body tampered without invalidating
the signature.

Anti-spoof invariant (``unwrap_inbound``):
  1. the envelope's ``source`` must equal the FQID the verifier authenticates the
     signature against — a peer cannot sign as itself and claim to be someone
     else (source-FQID binding);
  2. the signature must verify over the canonical bytes;
  3. a missing/empty signature is a hard rejection (no unsigned frames).

The capauth sign/verify backends are *injected* so this module carries no key
material and tests can drive it with in-memory fakes. A signer has the capauth
backend shape ``sign(data: bytes) -> str``; a verifier has
``verify(data: bytes, sig: str) -> str | None`` returning the authenticated
source FQID on success (or ``None`` / raising on failure).
"""

from __future__ import annotations

import base64
import json
from typing import Callable, Protocol

# A signer turns canonical bytes into a signature string (capauth armor).
Signer = Callable[[bytes], str]
# A verifier checks a signature over canonical bytes and returns the FQID the
# signature authenticates to (the true source), or None if it does not verify.
Verifier = Callable[[bytes, str], "str | None"]


class GatekeeperError(Exception):
    """Base class for inbound rejection errors."""


class MissingSignatureError(GatekeeperError):
    """The inbound envelope carried no (or an empty) signature."""


class MalformedEnvelopeError(GatekeeperError):
    """The inbound bytes were not a well-formed gatekeeper envelope."""


class SignatureVerificationError(GatekeeperError):
    """The signature did not verify over the canonical (source, frame) bytes."""


class SourceSpoofError(GatekeeperError):
    """The claimed source FQID did not match the signing identity (anti-spoof)."""


class _SignerProto(Protocol):
    def __call__(self, data: bytes) -> str: ...


def _canonical(source: str, frame: bytes) -> bytes:
    """Stable byte representation of the (source, frame) pair that gets signed.

    Binding ``source`` into the signed bytes is what prevents a valid frame from
    being lifted and re-broadcast under a different claimed source.
    """
    return json.dumps(
        {"source": source, "frame": base64.b64encode(frame).decode("ascii")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class GlossaMeshGatekeeper:
    """Signs outbound mesh frames and source-authenticates inbound ones.

    Args:
        source_fqid: this node's capauth FQID — stamped as ``source`` on every
            outbound envelope.
        signer: callable ``(canonical_bytes) -> signature``. Inject the capauth
            backend's bound ``sign`` here.
        verifier: callable ``(canonical_bytes, signature) -> authenticated_fqid``
            returning the FQID the signature belongs to, or ``None``/raising on
            failure. Inject the capauth backend's ``verify`` here.
    """

    def __init__(self, *, source_fqid: str, signer: Signer, verifier: Verifier) -> None:
        self.source_fqid = source_fqid
        self._signer = signer
        self._verifier = verifier

    def wrap_outbound(self, frame: bytes) -> bytes:
        """Sign ``frame`` under this node's identity, returning envelope bytes."""
        canonical = _canonical(self.source_fqid, frame)
        sig = self._signer(canonical)
        envelope = {
            "source": self.source_fqid,
            "frame": base64.b64encode(frame).decode("ascii"),
            "sig": sig,
        }
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    def unwrap_inbound(self, signed: bytes) -> tuple[str, bytes]:
        """Verify ``signed`` and return ``(source_fqid, frame_bytes)``.

        Raises a :class:`GatekeeperError` subclass on any failure: malformed
        envelope, missing signature, bad signature, or source-FQID spoof.
        """
        try:
            envelope = json.loads(signed.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise MalformedEnvelopeError(f"not a gatekeeper envelope: {exc}") from exc
        if not isinstance(envelope, dict):
            raise MalformedEnvelopeError("envelope is not an object")

        source = envelope.get("source")
        frame_b64 = envelope.get("frame")
        sig = envelope.get("sig")
        if not isinstance(source, str) or not isinstance(frame_b64, str):
            raise MalformedEnvelopeError("envelope missing source/frame")
        if not sig:
            raise MissingSignatureError("envelope carried no signature")

        try:
            frame = base64.b64decode(frame_b64.encode("ascii"), validate=True)
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise MalformedEnvelopeError(f"frame not valid base64: {exc}") from exc

        canonical = _canonical(source, frame)
        try:
            authenticated = self._verifier(canonical, sig)
        except Exception as exc:  # noqa: BLE001 — any backend failure == verify-fail
            raise SignatureVerificationError(f"verify raised: {exc}") from exc
        if not authenticated:
            raise SignatureVerificationError("signature did not verify")

        # Anti-spoof: the identity the signature authenticates to MUST be the
        # source the envelope claims. Otherwise a peer could sign as itself and
        # masquerade as another FQID.
        if authenticated != source:
            raise SourceSpoofError(
                f"claimed source {source!r} != signing identity {authenticated!r}"
            )
        return source, frame
