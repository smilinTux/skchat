"""Phase-3b Mode-C gift-wrap envelope — private, metadata-hiding invite delivery.

Implements ``docs/2026-07-15-sovereign-invite-join-architecture.md`` §4 step 2 +
hardening H6: the **per-recipient sealed envelope** (a NIP-59 "gift-wrap" borrow,
mapped onto skcomms) that privately delivers a Mode-C invite/accept payload to a
peer on an instance you have NOT federated with, over a **dumb, zero-trust
rendezvous relay** (Nostr discovery / Funnel).

The relay is availability only — it MUST learn neither *who* is talking to whom
nor *what* is said. So the envelope splits into two layers:

* **inner (sealed)** — the real ``{invite_token, sig, k, operator_reachability,
  sender, kind, true_ts, …}`` payload. Sealed to the recipient's advertised
  **hybrid** bundle with the vetted x25519 + ML-KEM-768 KEM — reused verbatim
  from :mod:`skchat.atrest_wrap` (``wrap_dek``/``unwrap_dek``), *not* reinvented.
  The real sender identity, the message ``kind``, and the true timestamp live
  ONLY here. HNDL-safe: a harvested envelope is not retroactively decryptable.
* **outer (cleartext, what the relay sees)** — a **fresh throwaway** signing key
  (NOT the sender identity), the recipient **fingerprint** tag only (never the
  full recipient key), a **randomized** ``created_at`` (never the true ts), and
  the sealed ``ciphertext``. Signed by the throwaway key so the wrap is a
  self-consistent event, exactly like a NIP-59 gift-wrap.

The outer metadata is additionally bound into the seal's AEAD **AAD**, so an
attacker cannot swap the recipient tag / created_at / throwaway key and re-sign
with their own throwaway key: any such mutation breaks the inner authentication.

Fail-closed is the rule (§5 oracle hygiene): a wrong recipient key, a tampered
ciphertext, a mutated outer, a bad throwaway signature, or the feature flag being
off all raise :class:`GiftwrapError` — the plaintext is NEVER partially returned
or leaked. Gated behind ``SKCHAT_PQ_INVITES_ENABLED`` (default off) via
:func:`skchat.pq_invites.pq_invites_enabled`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import struct
import time

from skchat import atrest_wrap as _atrest
from skchat import pq_invites as _pqi
from skchat.pq_invites import pq_invites_enabled

__all__ = [
    "GiftwrapError",
    "GIFTWRAP_VERSION",
    "GIFTWRAP_SUITE",
    "pq_invites_enabled",
    "recipient_fingerprint",
    "seal_giftwrap",
    "open_giftwrap",
]

#: Envelope format version (bump on any incompatible layout change).
GIFTWRAP_VERSION = 1
#: Suite label surfaced on the outer envelope (crypto-agility marker). The inner
#: seal is whatever :mod:`skchat.atrest_wrap` wraps with (``x25519-mlkem768``).
GIFTWRAP_SUITE = "giftwrap-x25519-mlkem768-v1"

#: Recipient fingerprint length in bytes (SHA-256 truncated → 128-bit tag). A
#: fingerprint, never the full recipient key (that would let the relay correlate
#: the recipient bundle across sends / directories).
_FP_LEN = 16

#: AES-GCM nonce length for the inner-payload seal (random per seal).
_INNER_NONCE_LEN = 12

#: Randomized-``created_at`` jitter window: up to this many seconds *before* the
#: relay-observable arrival, mirroring NIP-59 (the true send time never ships).
_CREATED_AT_JITTER = 2 * 24 * 3600  # 2 days


class GiftwrapError(Exception):
    """Gift-wrap seal/open failure — raised fail-closed, never leaks plaintext."""


# ── base64 helpers (standard, padded — envelope rides JSON) ──────────────────


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode((s or "").encode("ascii"))


# ── Recipient fingerprint (the outer ``p_tag``) ──────────────────────────────


def recipient_fingerprint(recipient_hybrid_pub_hex: str) -> str:
    """The outer ``p_tag`` — a short SHA-256 fingerprint of the recipient bundle.

    Never the full recipient public key: the relay only needs enough to route to
    the addressed peer, and shipping the whole bundle would let it correlate the
    recipient across sends. Fail-closed on a malformed hex key.
    """
    try:
        pub = bytes.fromhex((recipient_hybrid_pub_hex or "").strip())
    except ValueError as exc:
        raise GiftwrapError(f"bad recipient public key hex: {exc}") from exc
    if not pub:
        raise GiftwrapError("empty recipient public key")
    return hashlib.sha256(pub).digest()[:_FP_LEN].hex()


# ── Feature-flag gate (fail-closed) ──────────────────────────────────────────


def _require_enabled(op: str) -> None:
    if not pq_invites_enabled():
        raise GiftwrapError(
            f"{op}: SKCHAT_PQ_INVITES_ENABLED is off (gift-wrap disabled, fail-closed)"
        )


# ── AAD binding — the outer fields the inner seal authenticates ──────────────


def _aad(p_tag: str, created_at: int, throwaway_pub: str) -> bytes:
    """Canonical bytes of the outer metadata bound into the seal's AEAD AAD.

    Binding these means a relay/attacker cannot mutate the recipient tag, the
    randomized timestamp, or swap the throwaway key (and re-sign with its own)
    without breaking the inner authentication — tamper anywhere → fail-closed.
    """
    return _pqi._canonical(
        {
            "created_at": int(created_at),
            "p_tag": p_tag,
            "suite": GIFTWRAP_SUITE,
            "throwaway_pub": throwaway_pub,
            "v": GIFTWRAP_VERSION,
        }
    )


def _randomized_created_at() -> int:
    """A plausible but randomized ``created_at`` (NIP-59) — never the true ts.

    Arrival time is already observable to the relay; we jitter *backwards* by a
    random amount so the stamp reveals nothing correlatable and, critically, is
    never the inner payload's true timestamp.
    """
    return int(time.time()) - secrets.randbelow(_CREATED_AT_JITTER + 1)


# ── Throwaway outer signing key (Ed25519, fresh per envelope) ────────────────


def _load_ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )
    except Exception as exc:  # pragma: no cover — cryptography ships with pgpy in prod
        raise GiftwrapError(f"cryptography unavailable for throwaway signing: {exc}") from exc
    return Ed25519PrivateKey, Ed25519PublicKey, Encoding, PublicFormat


# ── Seal ─────────────────────────────────────────────────────────────────────


def seal_giftwrap(inner_payload: dict, recipient_hybrid_pub_hex: str) -> dict:
    """Seal *inner_payload* into a per-recipient gift-wrap envelope.

    The inner payload (real sender, kind, true ts, invite token, ``k`` fragment
    secret, …) is AES-256-GCM sealed under a fresh DEK that is hybrid-wrapped to
    ``recipient_hybrid_pub_hex`` (x25519 + ML-KEM-768, via
    :func:`skchat.atrest_wrap.wrap_dek`). The outer carries only a fresh throwaway
    Ed25519 key, the recipient fingerprint, a randomized ``created_at`` and the
    ciphertext, signed by the throwaway key.

    Args:
        inner_payload: The Mode-C payload to deliver privately (JSON-serializable).
        recipient_hybrid_pub_hex: The recipient's advertised 1216-byte hybrid
            public key, hex-encoded.

    Returns:
        A JSON-serializable envelope dict:
        ``{v, suite, p_tag, created_at, throwaway_pub, ciphertext, sig}``.

    Raises:
        GiftwrapError: if the feature flag is off, the recipient key is malformed,
            or the crypto backend is unavailable (fail-closed).
    """
    _require_enabled("seal_giftwrap")
    if not isinstance(inner_payload, dict):
        raise GiftwrapError("inner_payload must be a dict")

    try:
        recipient_pub = bytes.fromhex((recipient_hybrid_pub_hex or "").strip())
    except ValueError as exc:
        raise GiftwrapError(f"bad recipient public key hex: {exc}") from exc

    p_tag = recipient_fingerprint(recipient_hybrid_pub_hex)
    created_at = _randomized_created_at()

    Ed25519PrivateKey, _Pub, Encoding, PublicFormat = _load_ed25519()
    throwaway = Ed25519PrivateKey.generate()
    throwaway_pub = _b64e(
        throwaway.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )

    aad = _aad(p_tag, created_at, throwaway_pub)

    # Inner seal: fresh DEK → hybrid-wrapped to the recipient bundle (reuse the
    # vetted at-rest hybrid wrap), then AES-256-GCM the canonical inner under it.
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:  # pragma: no cover — cryptography ships with pgpy in prod
        raise GiftwrapError(f"cryptography unavailable for AEAD: {exc}") from exc

    dek = _atrest.new_dek()
    try:
        wrapped_dek = _atrest.wrap_dek(dek, recipient_pub)
    except Exception as exc:
        # Malformed key / missing KEM backend → never silently downgrade.
        raise GiftwrapError(f"hybrid wrap failed: {exc}") from exc

    plaintext = _pqi._canonical(inner_payload)
    nonce = os.urandom(_INNER_NONCE_LEN)
    sealed_inner = AESGCM(dek).encrypt(nonce, plaintext, aad)

    # ciphertext blob = u32(len(wrapped_dek)) || wrapped_dek || nonce || sealed_inner
    blob = struct.pack(">I", len(wrapped_dek)) + wrapped_dek + nonce + sealed_inner
    ciphertext = _b64e(blob)

    envelope = {
        "v": GIFTWRAP_VERSION,
        "suite": GIFTWRAP_SUITE,
        "p_tag": p_tag,
        "created_at": created_at,
        "throwaway_pub": throwaway_pub,
        "ciphertext": ciphertext,
    }
    # Outer throwaway signature over the canonical envelope-without-sig (NIP-59:
    # the wrap is a signed event). Anonymous by construction — the key is fresh.
    envelope["sig"] = _b64e(throwaway.sign(_pqi._canonical(envelope)))
    return envelope


# ── Open ─────────────────────────────────────────────────────────────────────


def open_giftwrap(envelope: dict, my_hybrid_priv_hex: str) -> dict:
    """Open a gift-wrap *envelope* addressed to me, returning the inner payload.

    Verifies the throwaway outer signature, unwraps the DEK with my hybrid
    private key, and AES-256-GCM-decrypts the inner (with the outer metadata bound
    as AAD). Any failure — wrong key, tampered ciphertext, mutated outer, bad
    signature, disabled flag — raises :class:`GiftwrapError` and NEVER returns a
    partial or plaintext leak.

    Args:
        envelope: An envelope produced by :func:`seal_giftwrap`.
        my_hybrid_priv_hex: My 2432-byte hybrid private key, hex-encoded.

    Returns:
        The inner payload dict.

    Raises:
        GiftwrapError: on any verification/decryption failure (fail-closed).
    """
    _require_enabled("open_giftwrap")
    if not isinstance(envelope, dict):
        raise GiftwrapError("envelope must be a dict")

    try:
        p_tag = envelope["p_tag"]
        created_at = envelope["created_at"]
        throwaway_pub = envelope["throwaway_pub"]
        ciphertext = envelope["ciphertext"]
        sig = envelope["sig"]
    except (KeyError, TypeError) as exc:
        raise GiftwrapError(f"malformed envelope (missing {exc})") from exc

    # 1. Verify the throwaway outer signature over the canonical envelope-sans-sig.
    Ed25519PrivateKey, Ed25519PublicKey, _Enc, _Fmt = _load_ed25519()
    signed = {k: v for k, v in envelope.items() if k != "sig"}
    try:
        Ed25519PublicKey.from_public_bytes(_b64d(throwaway_pub)).verify(
            _b64d(sig), _pqi._canonical(signed)
        )
    except Exception as exc:  # InvalidSignature / parse error → reject
        raise GiftwrapError(f"outer signature invalid: {exc}") from exc

    # 2. Parse the ciphertext blob and unwrap the DEK with my hybrid private key.
    try:
        priv = bytes.fromhex((my_hybrid_priv_hex or "").strip())
    except ValueError as exc:
        raise GiftwrapError(f"bad private key hex: {exc}") from exc

    try:
        blob = _b64d(ciphertext)
        (wrap_len,) = struct.unpack(">I", blob[:4])
        off = 4
        wrapped_dek = blob[off : off + wrap_len]
        off += wrap_len
        nonce = blob[off : off + _INNER_NONCE_LEN]
        off += _INNER_NONCE_LEN
        sealed_inner = blob[off:]
        if len(wrapped_dek) != wrap_len or len(nonce) != _INNER_NONCE_LEN or not sealed_inner:
            raise GiftwrapError("ciphertext blob truncated")
    except GiftwrapError:
        raise
    except Exception as exc:
        raise GiftwrapError(f"ciphertext blob malformed: {exc}") from exc

    try:
        dek = _atrest.unwrap_dek(wrapped_dek, priv)
    except Exception as exc:
        # Wrong recipient key / tampered wrap → fail-closed, no oracle detail.
        raise GiftwrapError("cannot open envelope (wrong key or tampered)") from exc

    # 3. AEAD-decrypt the inner, binding the (verified) outer metadata as AAD. A
    #    mutated p_tag/created_at/throwaway_pub breaks this even if re-signed.
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:  # pragma: no cover
        raise GiftwrapError(f"cryptography unavailable for AEAD: {exc}") from exc

    aad = _aad(p_tag, created_at, throwaway_pub)
    try:
        plaintext = AESGCM(dek).decrypt(nonce, sealed_inner, aad)
    except Exception as exc:
        raise GiftwrapError("cannot open envelope (wrong key or tampered)") from exc

    try:
        import json

        inner = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise GiftwrapError(f"inner payload not valid JSON: {exc}") from exc
    if not isinstance(inner, dict):
        raise GiftwrapError("inner payload is not a dict")
    return inner
