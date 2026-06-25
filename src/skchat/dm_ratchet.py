"""SKChat 1:1 DM epoch-ratchet — hybrid post-quantum direct-message keying.

This is **RFC-0001 P1** (``docs/rfcs/RFC-0001-pq-ratchet-metadata-dual-identity.md``
in skcomms): it lifts the 1:1 DM surface from the stateless one-shot hybrid seal
(:mod:`skchat.crypto` / :mod:`skcomms.pqdm` — Level 2, PQ only at the published
prekey, no running ratchet) to a **running epoch-ratchet** (Level 3) — the 1:1
analogue of :mod:`skchat.group_ratchet`.

Why Level 3
-----------
The one-shot seal re-encapsulates to the recipient's long-lived published prekey on
every message: there is no forward secrecy beyond prekey rotation and no
post-compromise security within a conversation. The DM ratchet adds both, the same
way the group ratchet does, but pairwise:

* The per-conversation **epoch secret** is distributed via the vetted hybrid KEM
  (``skcomms.pqkem`` — ``x25519-mlkem768``, HKDF(X25519 ‖ ML-KEM-768)) **once per
  epoch**, never per message — so the ~1.1 KB of ML-KEM ciphertext is amortised
  (the Apple-PQ3 / SimpleX insight: per-message PQ does not pay for itself).
* Per-message keys derive symmetrically from the epoch secret, index-addressable
  (loss/reorder tolerant); AES-256-GCM stays the only bulk cipher.
* **Periodic rekey** (50 messages OR 7 days) starts a fresh, independent epoch:
  forward secrecy across the boundary, post-compromise security (a leaked epoch
  secret reveals only its epoch — the next PQ rekey heals the channel).

Domain separation
-----------------
All HKDF labels are distinct from :mod:`skchat.group_ratchet` (``dm-ratchet`` vs
``group-ratchet``), so a DM key can never collide with a group key.
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

#: The crypto-suite id a conversation must carry to use this ratchet
#: (matches ``skcomms.crypto_suites`` / ``skcomms.pqkem.SUITE_ID``).
HYBRID_KEM_SUITE = "x25519-mlkem768"

#: Length of an epoch secret / a derived per-message key (bytes).
EPOCH_SECRET_LEN = 32
MESSAGE_KEY_LEN = 32

#: HKDF domain-separation labels — distinct from group_ratchet (never reuse).
_INFO_DM_WRAP = b"skchat/dm-ratchet/epoch-wrap/v1"
_INFO_DM_MESSAGE_KEY = b"skchat/dm-ratchet/msg/v1"

#: AES-GCM nonce length for the epoch-secret wrap (random per wrap).
_WRAP_NONCE_LEN = 12
#: Wrapped epoch secret = plaintext(32) + AES-GCM tag(16).
_WRAPPED_SECRET_LEN = EPOCH_SECRET_LEN + 16
#: Total per-conversation, per-epoch distribution payload size.
WRAPPED_PAYLOAD_LEN = HYBRID_CIPHERTEXT_LEN + _WRAP_NONCE_LEN + _WRAPPED_SECRET_LEN

#: Default re-key bounds (RFC-0001 P1 / Apple-PQ3: 50 messages OR 7 days).
DEFAULT_REKEY_MSG_BOUND = 50
DEFAULT_REKEY_AGE_SECONDS = 7 * 24 * 3600


class DmRatchetError(Exception):
    """Base error for the 1:1 DM epoch-ratchet."""


def _u64(n: int) -> bytes:
    return struct.pack(">Q", n & 0xFFFFFFFFFFFFFFFF)


def _epoch_salt(epoch: int) -> bytes:
    """Domain-separate message keys per epoch via the HKDF salt."""
    return b"skchat/dm-epoch/" + _u64(epoch)


def derive_dm_message_key(epoch_secret: bytes, epoch: int, index: int) -> bytes:
    """Derive the AES-256 key for DM message ``index`` in ``epoch``.

    Deterministic and index-addressable (loss/reorder tolerant): the same
    (epoch_secret, epoch, index) always yields the same 32-byte key, and any
    index can be derived independently of the others.

    Args:
        epoch_secret: The 32-byte secret for this conversation epoch.
        epoch: The epoch number (folded into the HKDF salt).
        index: Zero-based message index within the epoch.

    Returns:
        A 32-byte AES-256 message key.
    """
    if len(epoch_secret) != EPOCH_SECRET_LEN:
        raise DmRatchetError(
            f"epoch_secret must be {EPOCH_SECRET_LEN} bytes, got {len(epoch_secret)}"
        )
    info = _INFO_DM_MESSAGE_KEY + b"/" + _u64(index)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=MESSAGE_KEY_LEN,
        salt=_epoch_salt(epoch),
        info=info,
    ).derive(epoch_secret)


# ---------------------------------------------------------------------------
# Epoch-secret distribution (hybrid KEM, once per epoch)
# ---------------------------------------------------------------------------


def new_epoch_secret() -> bytes:
    """Generate a fresh random 32-byte epoch secret (independent of any prior)."""
    return os.urandom(EPOCH_SECRET_LEN)


def wrap_dm_epoch_secret(epoch_secret: bytes, peer_hybrid_pub: bytes) -> bytes:
    """Wrap an epoch secret to the peer's hybrid-KEM public key.

    Uses ``hybrid_encap`` (X25519 ‖ ML-KEM-768) for a one-time shared secret,
    HKDF-expands it to an AES-256 wrap key, and AES-256-GCM-encrypts the epoch
    secret. The KEM ciphertext travels in the blob so the peer can decapsulate —
    this PQ material is the per-epoch cost (NOT per message).

    Args:
        epoch_secret: 32-byte epoch secret.
        peer_hybrid_pub: Peer's 1216-byte hybrid public key.

    Returns:
        ``hybrid_ct(1120) || nonce(12) || wrapped(48)`` bytes.

    Raises:
        DmRatchetError: on malformed inputs.
        PqKemError / PqKemUnavailable: if the hybrid KEM backend is missing or the
            public key is malformed (propagated — never silently downgraded).
    """
    if len(epoch_secret) != EPOCH_SECRET_LEN:
        raise DmRatchetError(
            f"epoch_secret must be {EPOCH_SECRET_LEN} bytes, got {len(epoch_secret)}"
        )
    if len(peer_hybrid_pub) != HYBRID_PUBLIC_KEY_LEN:
        raise DmRatchetError(
            f"peer hybrid public key must be {HYBRID_PUBLIC_KEY_LEN} bytes, "
            f"got {len(peer_hybrid_pub)}"
        )

    ciphertext, shared = hybrid_encap(peer_hybrid_pub)
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=_INFO_DM_WRAP,
    ).derive(shared)
    nonce = os.urandom(_WRAP_NONCE_LEN)
    wrapped = AESGCM(wrap_key).encrypt(nonce, epoch_secret, None)
    return ciphertext + nonce + wrapped


def unwrap_dm_epoch_secret(payload: bytes, peer_hybrid_priv: bytes) -> bytes:
    """Recover an epoch secret from a wrapped payload using the peer's private key.

    Args:
        payload: ``hybrid_ct(1120) || nonce(12) || wrapped(48)`` from
            :func:`wrap_dm_epoch_secret`.
        peer_hybrid_priv: The recipient's 2432-byte hybrid private key.

    Returns:
        The 32-byte epoch secret.

    Raises:
        DmRatchetError: on malformed payload or authentication failure.
        PqKemError / PqKemUnavailable: if the hybrid KEM backend is missing.
    """
    if len(payload) != WRAPPED_PAYLOAD_LEN:
        raise DmRatchetError(
            f"wrapped epoch payload must be {WRAPPED_PAYLOAD_LEN} bytes, "
            f"got {len(payload)}"
        )
    ciphertext = payload[:HYBRID_CIPHERTEXT_LEN]
    nonce = payload[HYBRID_CIPHERTEXT_LEN : HYBRID_CIPHERTEXT_LEN + _WRAP_NONCE_LEN]
    wrapped = payload[HYBRID_CIPHERTEXT_LEN + _WRAP_NONCE_LEN :]

    shared = hybrid_decap(ciphertext, peer_hybrid_priv)
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=_INFO_DM_WRAP,
    ).derive(shared)
    try:
        return AESGCM(wrap_key).decrypt(nonce, wrapped, None)
    except Exception as exc:  # GCM auth failure / wrong key
        raise DmRatchetError(f"dm epoch-secret unwrap failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Ratchet state
# ---------------------------------------------------------------------------


@dataclass
class DmRatchet:
    """In-memory ratchet state for one 1:1 conversation epoch (sender or receiver).

    A conversation's authoritative state is the current ``epoch`` number + the
    current ``epoch_secret``; per-message keys are derived on demand. The sender's
    ``message_index`` is its monotone counter for the *next* message it will send
    in this epoch; the receiver ignores its own counter and uses each message's
    carried ``(epoch, index)`` (index-addressable → loss/reorder tolerant).

    Periodic rekey (``should_rekey``) starts a fresh epoch with an independent
    secret — forward secrecy across the boundary, post-compromise security within.

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
        return derive_dm_message_key(self.epoch_secret, self.epoch, idx)

    def next_outbound_key(self) -> tuple[int, bytes]:
        """Return ``(index, key)`` for the next message to send and advance.

        The returned ``index`` MUST be placed on the wire so the peer derives the
        same key. Advancing the counter gives intra-epoch ordering; it does NOT
        gate decryption (the peer is index-addressed).
        """
        idx = self.message_index
        key = derive_dm_message_key(self.epoch_secret, self.epoch, idx)
        self.message_index += 1
        return idx, key

    def should_rekey(self, now: Optional[float] = None) -> bool:
        """Whether the bound (msg count OR age) says this epoch should re-key."""
        if self.message_index >= self.rekey_msg_bound:
            return True
        t = time.time() if now is None else now
        return (t - self.epoch_started_at) >= self.rekey_age_seconds
