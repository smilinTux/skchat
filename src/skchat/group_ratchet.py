"""SKChat group epoch-ratchet — hybrid post-quantum group key distribution.

This is **Phase 1 / Q2** of the PQC-MIGRATION epic (coord ``e1d6ba2a``; plan
``docs/quantum-resistance-architecture.md`` §3 S5, §5 Phase 1). It is the marquee
HNDL fix: it replaces the *static* ``os.urandom(32)`` group key (PGP-wrapped per
member, in :mod:`skchat.group`) with a **per-epoch ratchet** whose epoch secret is
distributed via the vetted hybrid KEM primitive ``skcomms.pqkem``
(``x25519-mlkem768`` — HKDF(X25519 ‖ ML-KEM-768), liboqs-backed).

Why this kills the highest-leverage quantum vulnerability
---------------------------------------------------------
The Q0 group wrapped a single long-lived AES key with each member's classical PGP
key. Break **one** member's classical key (now, or post-CRQC against harvested
ciphertext) and you recover the AES key -> decrypt **all** group history. The
epoch-ratchet breaks that:

* The epoch secret is wrapped to each member with a **hybrid** KEM — secret unless
  *both* X25519 **and** ML-KEM-768 are broken (HNDL-resistant).
* Each epoch has its own independent secret. A leaked epoch secret reveals only
  that epoch (post-compromise security, PCS).
* Re-keying on member add/remove gives **forward secrecy** (FS): a removed member
  cannot derive any future epoch's keys.

Design — two layers
-------------------
1. **Epoch distribution (asymmetric, hybrid-KEM, ONCE PER EPOCH).**
   For each member holding a hybrid-KEM public key (1216 B,
   ``skcomms.pqkem.PUBLIC_KEY_LEN``), the epoch secret (32 B) is wrapped::

       ct, ss      = hybrid_encap(member_pub)         # PQ material — once/epoch
       wrap_key    = HKDF(ss, info=b"…/epoch-wrap/v1")
       wrapped     = AES-256-GCM(wrap_key).encrypt(nonce, epoch_secret)
       payload     = ct(1120) || nonce(12) || wrapped(48)   # 1180 B / member / epoch

   The 1.1 KB of ML-KEM ciphertext is paid **once per epoch**, NOT per message —
   this is what avoids the 33x-per-message ML-KEM bandwidth bloat called out in
   the plan (§5 Phase-1 risk).

2. **Per-message keys (symmetric KDF ratchet, NO PQ material).**
   Message keys are derived from the epoch secret by a symmetric HKDF ratchet, so
   individual messages have forward secrecy *within* an epoch and AES-256-GCM
   stays the only bulk cipher::

       message_key(i) = HKDF(epoch_secret, salt=epoch_no, info=b"…/msg/" + i)

   Keys are derived **directly by index** (not by mutating a running chain), so the
   scheme is fully **loss- and reorder-tolerant**: a receiver can derive the key
   for message *i* without having seen messages 0..i-1, and out-of-order arrival
   needs no buffering. (A forward-only running-chain variant would zeroise prior
   keys for stronger intra-epoch FS but breaks reorder tolerance; we keep epochs
   short — 50 msgs / 7 days — and rely on the epoch boundary for FS instead.)

Crypto-agility / honesty
------------------------
Suite id ``x25519-mlkem768`` resolves in ``skcomms.crypto_suites`` as
``hybrid-pq``. A group only uses this ratchet when its ``kem_suite`` is the hybrid
suite; classical (``rsa-pgp-wrap-v1``) groups are untouched. The hybrid KEM is a
hard dependency *only* for hybrid groups — if liboqs is missing, hybrid operations
raise loudly (never a silent classical downgrade); classical groups keep working.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

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
    hybrid_decap,
    hybrid_encap,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The crypto-suite id a group must carry to use this ratchet (matches
#: ``skcomms.crypto_suites`` / ``skcomms.pqkem.SUITE_ID``).
HYBRID_KEM_SUITE = "x25519-mlkem768"

#: Length of an epoch secret / a derived per-message key (bytes).
EPOCH_SECRET_LEN = 32
MESSAGE_KEY_LEN = 32

#: HKDF domain-separation labels (never reuse across layers).
_INFO_EPOCH_WRAP = b"skchat/group-ratchet/epoch-wrap/v1"
_INFO_MESSAGE_KEY = b"skchat/group-ratchet/msg/v1"

#: AES-GCM nonce length for the epoch-secret wrap (random per wrap).
_WRAP_NONCE_LEN = 12
#: Wrapped epoch secret = plaintext(32) + AES-GCM tag(16).
_WRAPPED_SECRET_LEN = EPOCH_SECRET_LEN + 16

#: Total per-member, per-epoch distribution payload size.
WRAPPED_PAYLOAD_LEN = HYBRID_CIPHERTEXT_LEN + _WRAP_NONCE_LEN + _WRAPPED_SECRET_LEN

#: Default re-key bounds (§5 Phase 1: 50 messages OR 7 days).
DEFAULT_REKEY_MSG_BOUND = 50
DEFAULT_REKEY_AGE_SECONDS = 7 * 24 * 3600


class GroupRatchetError(Exception):
    """Base error for the group epoch-ratchet."""


class MissingHybridKeyError(GroupRatchetError):
    """A member has no hybrid-KEM public key — cannot receive the epoch secret.

    Raised only when distribution is asked to be strict. The default
    distribution path skips such members gracefully (documented fallback) so a
    mixed group never hard-fails; the self-report flags the gap.
    """


# ---------------------------------------------------------------------------
# Per-message key derivation (symmetric ratchet — no PQ material)
# ---------------------------------------------------------------------------


def _u64(n: int) -> bytes:
    return struct.pack(">Q", n & 0xFFFFFFFFFFFFFFFF)


def _epoch_salt(epoch: int) -> bytes:
    """Domain-separate message keys per epoch via the HKDF salt."""
    return b"skchat/epoch/" + _u64(epoch)


def derive_message_key(epoch_secret: bytes, epoch: int, index: int) -> bytes:
    """Derive the AES-256 key for message ``index`` in ``epoch``.

    Deterministic and index-addressable (loss/reorder tolerant): the same
    (epoch_secret, epoch, index) always yields the same 32-byte key, and any
    index can be derived independently of the others.

    Args:
        epoch_secret: The 32-byte secret for this epoch.
        epoch: The epoch number (folded into the HKDF salt).
        index: Zero-based message index within the epoch.

    Returns:
        A 32-byte AES-256 message key.
    """
    if len(epoch_secret) != EPOCH_SECRET_LEN:
        raise GroupRatchetError(
            f"epoch_secret must be {EPOCH_SECRET_LEN} bytes, got {len(epoch_secret)}"
        )
    info = _INFO_MESSAGE_KEY + b"/" + _u64(index)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=MESSAGE_KEY_LEN,
        salt=_epoch_salt(epoch),
        info=info,
    ).derive(epoch_secret)


# ---------------------------------------------------------------------------
# Epoch-secret wrapping (hybrid KEM, once per epoch per member)
# ---------------------------------------------------------------------------


def new_epoch_secret() -> bytes:
    """Generate a fresh random 32-byte epoch secret."""
    return os.urandom(EPOCH_SECRET_LEN)


def wrap_epoch_secret(epoch_secret: bytes, member_hybrid_pub: bytes) -> bytes:
    """Wrap an epoch secret to a single member's hybrid-KEM public key.

    Uses ``hybrid_encap`` (X25519 ‖ ML-KEM-768) to derive a one-time shared
    secret, HKDF-expands it to an AES-256 wrap key, and AES-256-GCM-encrypts the
    epoch secret. The returned blob carries the KEM ciphertext so the recipient
    can decapsulate — this PQ material is the per-epoch cost (NOT per message).

    Args:
        epoch_secret: 32-byte epoch secret.
        member_hybrid_pub: Member's 1216-byte hybrid public key.

    Returns:
        ``hybrid_ct(1120) || nonce(12) || wrapped(48)`` bytes.

    Raises:
        GroupRatchetError: on malformed inputs.
        PqKemError / PqKemUnavailable: if the hybrid KEM backend is missing or
            the public key is malformed (propagated — never silently downgraded).
    """
    if len(epoch_secret) != EPOCH_SECRET_LEN:
        raise GroupRatchetError(
            f"epoch_secret must be {EPOCH_SECRET_LEN} bytes, got {len(epoch_secret)}"
        )
    if len(member_hybrid_pub) != HYBRID_PUBLIC_KEY_LEN:
        raise GroupRatchetError(
            f"member hybrid public key must be {HYBRID_PUBLIC_KEY_LEN} bytes, "
            f"got {len(member_hybrid_pub)}"
        )

    ciphertext, shared = hybrid_encap(member_hybrid_pub)
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=_INFO_EPOCH_WRAP,
    ).derive(shared)
    nonce = os.urandom(_WRAP_NONCE_LEN)
    wrapped = AESGCM(wrap_key).encrypt(nonce, epoch_secret, None)
    return ciphertext + nonce + wrapped


def unwrap_epoch_secret(payload: bytes, member_hybrid_priv: bytes) -> bytes:
    """Recover an epoch secret from a wrapped payload using the member's key.

    Args:
        payload: ``hybrid_ct(1120) || nonce(12) || wrapped(48)`` from
            :func:`wrap_epoch_secret`.
        member_hybrid_priv: Member's 2432-byte hybrid private key.

    Returns:
        The 32-byte epoch secret.

    Raises:
        GroupRatchetError: on malformed payload or authentication failure.
        PqKemError / PqKemUnavailable: if the hybrid KEM backend is missing.
    """
    if len(payload) != WRAPPED_PAYLOAD_LEN:
        raise GroupRatchetError(
            f"wrapped epoch payload must be {WRAPPED_PAYLOAD_LEN} bytes, "
            f"got {len(payload)}"
        )
    ciphertext = payload[:HYBRID_CIPHERTEXT_LEN]
    nonce = payload[
        HYBRID_CIPHERTEXT_LEN : HYBRID_CIPHERTEXT_LEN + _WRAP_NONCE_LEN
    ]
    wrapped = payload[HYBRID_CIPHERTEXT_LEN + _WRAP_NONCE_LEN :]

    shared = hybrid_decap(ciphertext, member_hybrid_priv)
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=_INFO_EPOCH_WRAP,
    ).derive(shared)
    try:
        return AESGCM(wrap_key).decrypt(nonce, wrapped, None)
    except Exception as exc:  # GCM auth failure / wrong key
        raise GroupRatchetError(f"epoch-secret unwrap failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Ratchet state
# ---------------------------------------------------------------------------


@dataclass
class EpochRatchet:
    """In-memory ratchet state for one group epoch (sender or receiver side).

    A group's authoritative state is the current ``epoch`` number + the current
    ``epoch_secret``; per-message keys are derived on demand. ``message_index``
    is the sender's monotone counter for the *next* message it will send in this
    epoch. Receivers ignore their own counter and use each message's carried
    ``(epoch, index)``.

    Attributes:
        epoch: Current epoch number.
        epoch_secret: 32-byte secret for the current epoch.
        message_index: Next outbound message index in this epoch.
        rekey_msg_bound: Re-key after this many messages in an epoch.
        rekey_age_seconds: Re-key after the epoch is this old.
        epoch_started_at: Wall-clock creation time (epoch seconds) of the epoch.
    """

    epoch: int
    epoch_secret: bytes
    message_index: int = 0
    rekey_msg_bound: int = DEFAULT_REKEY_MSG_BOUND
    rekey_age_seconds: int = DEFAULT_REKEY_AGE_SECONDS
    epoch_started_at: float = field(default_factory=time.time)

    def message_key(self, index: Optional[int] = None) -> bytes:
        """Derive the message key for ``index`` (default: the next outbound)."""
        idx = self.message_index if index is None else index
        return derive_message_key(self.epoch_secret, self.epoch, idx)

    def next_outbound_key(self) -> tuple[int, bytes]:
        """Return ``(index, key)`` for the next message to send and advance.

        The returned ``index`` MUST be placed on the wire so receivers derive the
        same key. Advancing the counter is what gives intra-epoch ordering; it
        does NOT gate decryption (receivers are index-addressed).
        """
        idx = self.message_index
        key = derive_message_key(self.epoch_secret, self.epoch, idx)
        self.message_index += 1
        return idx, key

    def should_rekey(self, now: Optional[float] = None) -> bool:
        """Whether the bound (msg count OR age) says this epoch should re-key."""
        if self.message_index >= self.rekey_msg_bound:
            return True
        t = time.time() if now is None else now
        return (t - self.epoch_started_at) >= self.rekey_age_seconds
