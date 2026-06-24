"""At-rest hybrid key-wrap — **X25519 + ML-KEM-768** DEK sealing for long-lived data.

This is **Phase 1 / Q4** of the PQC-MIGRATION epic (coord ``e1d6ba2a``; plan
``docs/quantum-resistance-architecture.md`` §3 S11 / §5 Phase 1 / §6 Q4). It is the
**at-rest** counterpart to Q2's group epoch-ratchet: instead of wrapping a per-epoch
group secret, it wraps a **data-encryption key (DEK)** for long-lived data at rest
(chat stores, AI-LIFE content, memory trees, skmem-pg dumps, the capauth root backup)
with the vetted hybrid KEM primitive :mod:`skcomms.pqkem` (``x25519-mlkem768`` —
``HKDF(X25519 ‖ ML-KEM-768)``, liboqs-backed).

Why this kills the at-rest HNDL vulnerability
---------------------------------------------
The bulk cipher for at-rest data is already AES-256-GCM (Grover-only, ~128-bit,
quantum-acceptable — we do NOT touch it). The *only* asymmetric exposure is how the
DEK is wrapped. If the DEK is wrapped with a classical recipient key, an adversary who
**harvests the encrypted backup today** can decrypt it **after a CRQC exists** (HNDL).
For decade-secrecy data (Mosca's Inequality already breached) this is the prime target.

The fix: wrap the DEK with a **hybrid** KEM so the wrapped DEK stays secret unless
*both* X25519 **and** ML-KEM-768 are broken. A harvested backup is not retroactively
decryptable. The DEK itself is high-entropy random key material (``os.urandom(32)``) —
**never** derived from a low-entropy / public value like a PGP fingerprint.

Construction (mirrors :func:`skchat.group_ratchet.wrap_epoch_secret` exactly)
-----------------------------------------------------------------------------
::

    ct, ss   = hybrid_encap(recipient_pub)             # PQ material — once per DEK
    wrap_key = HKDF-SHA256(ss, info=b"skchat/atrest-wrap/dek/v1")
    nonce    = os.urandom(12)
    wrapped  = AES-256-GCM(wrap_key).encrypt(nonce, dek)
    blob     = MAGIC || version(1) || suite_len || suite_id
                    || hybrid_ct(1120) || nonce(12) || wrapped(48)

We compose ONLY vetted primitives — ``hybrid_encap``/``hybrid_decap`` (skcomms.pqkem),
HKDF-SHA256 (pyca), AES-256-GCM (pyca). No lattice/curve/AEAD math is re-implemented
here. (``age`` 1.3 hybrid recipients were considered per the plan; ``age`` is not
installed and a bare classical ``age`` recipient would NOT be hybrid, so we hand-compose
the same hybrid construction Q2 already ships — one fewer dependency, identical idiom.)

Crypto-agility (Q0)
-------------------
The blob is **suite-tagged and versioned**: ``MAGIC``+``WRAP_FORMAT_VERSION`` and an
embedded ``suite_id`` string (default ``x25519-mlkem768``). A future suite (parameter
bump, HQC backup KEM, a broken-primitive swap) becomes a new suite id + a version,
never a flag-day. :func:`describe_blob` reports the suite without unwrapping (for the
PQC self-report).

Honesty / fallback: the hybrid KEM is a hard dependency. If liboqs (``oqs``) is missing,
:func:`wrap_dek`/:func:`unwrap_dek` raise loudly (via ``skcomms.pqkem``); they NEVER
silently downgrade to a classical-only wrap.
"""

from __future__ import annotations

import os
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from skcomms.pqkem import (
    CIPHERTEXT_LEN as HYBRID_CIPHERTEXT_LEN,
)
from skcomms.pqkem import (
    PUBLIC_KEY_LEN as HYBRID_PUBLIC_KEY_LEN,
)
from skcomms.pqkem import (
    SUITE_ID as HYBRID_SUITE_ID,
)
from skcomms.pqkem import (
    hybrid_decap,
    hybrid_encap,
    hybrid_keypair,
)

__all__ = [
    "AtRestWrapError",
    "AtRestWrapFormatError",
    "WRAP_FORMAT_VERSION",
    "WRAP_MAGIC",
    "DEK_LEN",
    "wrap_dek",
    "unwrap_dek",
    "new_dek",
    "new_recipient_keypair",
    "describe_blob",
    "is_wrapped_blob",
]

# ---------------------------------------------------------------------------
# Format constants — versioned + suite-tagged (Q0 crypto-agility).
# ---------------------------------------------------------------------------

#: Magic prefix so a wrapped blob is self-identifying (and old-format detection
#: in callers is unambiguous).
WRAP_MAGIC = b"SKAW"  # SK At-rest Wrap
#: Bump on any incompatible format change. v1 = hybrid X25519+ML-KEM-768.
WRAP_FORMAT_VERSION = 1

#: The hybrid KEM suite id this module wraps with (matches skcomms.pqkem).
DEFAULT_SUITE_ID = HYBRID_SUITE_ID  # "x25519-mlkem768"

#: A data-encryption key is a 32-byte AES-256 key.
DEK_LEN = 32

#: HKDF domain-separation label for the wrap key (never reuse across layers).
_INFO_DEK_WRAP = b"skchat/atrest-wrap/dek/v1"

#: AES-GCM nonce length for the DEK wrap (random per wrap).
_WRAP_NONCE_LEN = 12
#: Wrapped DEK = plaintext(32) + AES-GCM tag(16).
_WRAPPED_DEK_LEN = DEK_LEN + 16


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AtRestWrapError(Exception):
    """Base error for the at-rest hybrid key-wrap layer."""


class AtRestWrapFormatError(AtRestWrapError, ValueError):
    """Malformed wrap blob, wrong magic/version, or bad input length."""


# ---------------------------------------------------------------------------
# Key + DEK material helpers
# ---------------------------------------------------------------------------


def new_dek() -> bytes:
    """Generate a fresh high-entropy 32-byte data-encryption key.

    This is the DEK source the at-rest store MUST use — ``os.urandom(32)``,
    NEVER a value derived from a fingerprint or other low-entropy/public input.
    """
    return os.urandom(DEK_LEN)


def new_recipient_keypair():
    """Generate a fresh hybrid recipient keypair (X25519 ‖ ML-KEM-768).

    Returns a ``skcomms.pqkem.HybridKeyPair`` whose ``private_key`` is the
    long-lived secret that protects every DEK wrapped to ``public_key``. Treat
    the private key as a root secret (store 0600, back it up under its own wrap).

    Raises:
        PqKemUnavailable: if the liboqs/``oqs`` backend is missing.
    """
    return hybrid_keypair()


# ---------------------------------------------------------------------------
# Wrap / unwrap (hybrid KEM, one KEM op per DEK)
# ---------------------------------------------------------------------------


def _pack_header(suite_id: str) -> bytes:
    suite_bytes = suite_id.encode("utf-8")
    if len(suite_bytes) > 0xFFFF:
        raise AtRestWrapFormatError("suite id too long")
    return WRAP_MAGIC + struct.pack(">BH", WRAP_FORMAT_VERSION, len(suite_bytes)) + suite_bytes


def _parse_header(blob: bytes) -> tuple[str, int]:
    """Validate magic/version and return ``(suite_id, body_offset)``."""
    if not isinstance(blob, (bytes, bytearray)):
        raise AtRestWrapFormatError(
            f"wrap blob must be bytes, got {type(blob).__name__}"
        )
    head_len = len(WRAP_MAGIC) + 3  # magic + version(1) + suite_len(2)
    if len(blob) < head_len:
        raise AtRestWrapFormatError("wrap blob truncated (header)")
    if bytes(blob[: len(WRAP_MAGIC)]) != WRAP_MAGIC:
        raise AtRestWrapFormatError("not a SKAW wrap blob (bad magic)")
    version, suite_len = struct.unpack(
        ">BH", bytes(blob[len(WRAP_MAGIC) : head_len])
    )
    if version != WRAP_FORMAT_VERSION:
        raise AtRestWrapFormatError(
            f"unsupported wrap format version {version} (expected {WRAP_FORMAT_VERSION})"
        )
    suite_end = head_len + suite_len
    if len(blob) < suite_end:
        raise AtRestWrapFormatError("wrap blob truncated (suite id)")
    suite_id = bytes(blob[head_len:suite_end]).decode("utf-8", errors="replace")
    return suite_id, suite_end


def wrap_dek(
    dek: bytes,
    recipient_hybrid_pub: bytes,
    suite_id: str = DEFAULT_SUITE_ID,
) -> bytes:
    """Wrap a DEK to a recipient's hybrid public key (HNDL-resistant).

    Encapsulates a one-time shared secret to ``recipient_hybrid_pub`` with the
    hybrid X25519+ML-KEM-768 KEM, HKDF-expands it to an AES-256 wrap key, and
    AES-256-GCM-seals the DEK. The returned blob carries the KEM ciphertext so the
    recipient (private-key holder) can decapsulate. Secret unless BOTH primitives break.

    Args:
        dek: The 32-byte data-encryption key to seal (use :func:`new_dek`).
        recipient_hybrid_pub: Recipient's 1216-byte hybrid public key.
        suite_id: Suite tag embedded in the blob (default ``x25519-mlkem768``).

    Returns:
        A self-describing, versioned, suite-tagged wrap blob (bytes).

    Raises:
        AtRestWrapFormatError: on malformed inputs.
        PqKemError / PqKemUnavailable: if the hybrid KEM backend is missing or the
            public key is malformed (propagated — never silently downgraded).
    """
    if not isinstance(dek, (bytes, bytearray)) or len(dek) != DEK_LEN:
        raise AtRestWrapFormatError(
            f"dek must be {DEK_LEN} bytes, got "
            f"{len(dek) if isinstance(dek, (bytes, bytearray)) else type(dek).__name__}"
        )
    if len(recipient_hybrid_pub) != HYBRID_PUBLIC_KEY_LEN:
        raise AtRestWrapFormatError(
            f"recipient hybrid public key must be {HYBRID_PUBLIC_KEY_LEN} bytes, "
            f"got {len(recipient_hybrid_pub)}"
        )

    ciphertext, shared = hybrid_encap(bytes(recipient_hybrid_pub))
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=_INFO_DEK_WRAP,
    ).derive(shared)
    nonce = os.urandom(_WRAP_NONCE_LEN)
    wrapped = AESGCM(wrap_key).encrypt(nonce, bytes(dek), None)
    return _pack_header(suite_id) + ciphertext + nonce + wrapped


def unwrap_dek(blob: bytes, recipient_hybrid_priv: bytes) -> bytes:
    """Recover a DEK from a wrap blob using the recipient's hybrid private key.

    Args:
        blob: A blob produced by :func:`wrap_dek`.
        recipient_hybrid_priv: Recipient's 2432-byte hybrid private key.

    Returns:
        The 32-byte DEK.

    Raises:
        AtRestWrapFormatError: on bad magic/version, malformed/truncated blob, or
            authentication failure (wrong key / tampered blob).
        PqKemError / PqKemUnavailable: if the hybrid KEM backend is missing.
    """
    suite_id, body_offset = _parse_header(blob)  # validates magic/version
    body = bytes(blob[body_offset:])
    expected_body = HYBRID_CIPHERTEXT_LEN + _WRAP_NONCE_LEN + _WRAPPED_DEK_LEN
    if len(body) != expected_body:
        raise AtRestWrapFormatError(
            f"wrap body for suite {suite_id!r} must be {expected_body} bytes, "
            f"got {len(body)}"
        )

    ciphertext = body[:HYBRID_CIPHERTEXT_LEN]
    nonce = body[HYBRID_CIPHERTEXT_LEN : HYBRID_CIPHERTEXT_LEN + _WRAP_NONCE_LEN]
    wrapped = body[HYBRID_CIPHERTEXT_LEN + _WRAP_NONCE_LEN :]

    shared = hybrid_decap(ciphertext, bytes(recipient_hybrid_priv))
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=_INFO_DEK_WRAP,
    ).derive(shared)
    try:
        dek = AESGCM(wrap_key).decrypt(nonce, wrapped, None)
    except Exception as exc:  # GCM auth failure / wrong key / tampered blob
        raise AtRestWrapFormatError(f"DEK unwrap failed: {exc}") from exc
    if len(dek) != DEK_LEN:
        raise AtRestWrapFormatError("unwrapped DEK has wrong length")
    return dek


# ---------------------------------------------------------------------------
# Introspection (for the PQC self-report — no private key needed)
# ---------------------------------------------------------------------------


def is_wrapped_blob(blob: bytes) -> bool:
    """Cheap check: does ``blob`` look like a SKAW wrap blob (magic match)?"""
    return (
        isinstance(blob, (bytes, bytearray))
        and len(blob) >= len(WRAP_MAGIC)
        and bytes(blob[: len(WRAP_MAGIC)]) == WRAP_MAGIC
    )


def describe_blob(blob: bytes) -> dict:
    """Return ``{suite_id, version, kem_ciphertext_len, total_len}`` for a blob.

    Reads only the header — does NOT decapsulate or need the private key. Used by
    the at-rest surface self-report to state the wrap suite from a stored blob.
    """
    suite_id, body_offset = _parse_header(blob)
    return {
        "magic": WRAP_MAGIC.decode("ascii"),
        "version": WRAP_FORMAT_VERSION,
        "suite_id": suite_id,
        "kem_ciphertext_len": HYBRID_CIPHERTEXT_LEN,
        "total_len": len(blob),
    }
