"""SKChat 1:1 DM ratchet session driver (RFC-0001 P1 — Level-3 periodic PQ rekey).

:class:`DmSession` is the stateful layer on top of the pure
:class:`skchat.dm_ratchet.DmRatchet` primitive. It owns the **epoch lifecycle** for
one peer:

* **Auto-(re)key.** The first :meth:`seal` establishes epoch 0; once an epoch hits
  its bound (``rekey_msg_bound`` messages OR ``rekey_age_seconds``) the next
  :meth:`seal` starts a fresh epoch with an independent secret — forward secrecy
  across the boundary, post-compromise security (the PQ rekey heals the channel).
* **Key-agreement message (KAM) piggyback.** The wrapped epoch secret rides on the
  *first frame of each epoch* (:attr:`SealedDmFrame.kam`), so the sender never waits
  a round-trip to start sending — the receiver builds its ratchet from that frame.
* **Per-epoch secret store.** Both sides keep ``{epoch: secret}``, so frames are
  loss/reorder tolerant *across* epochs too: a frame for any epoch whose KAM has
  been seen opens by ``(epoch, index)``.

The per-frame body is AES-256-GCM with the ``(epoch, index)`` bound into the AAD,
so a frame can't be replayed into another slot. Pure state machine — no I/O; the
transport/persistence wiring is a thin adapter on top.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from skchat.dm_ratchet import (
    DEFAULT_REKEY_AGE_SECONDS,
    DEFAULT_REKEY_MSG_BOUND,
    DmRatchet,
    DmRatchetError,
    derive_dm_message_key,
    new_epoch_secret,
    unwrap_dm_epoch_secret,
    wrap_dm_epoch_secret,
)

_FRAME_NONCE_LEN = 12
_AAD_PREFIX = b"skchat/dm-frame/v1"


def _frame_aad(epoch: int, index: int) -> bytes:
    """Bind (epoch, index) into the AEAD AAD so a frame can't move slots."""
    return _AAD_PREFIX + b"|" + struct.pack(">QQ", epoch & 0xFFFFFFFFFFFFFFFF, index & 0xFFFFFFFFFFFFFFFF)


@dataclass
class SealedDmFrame:
    """One sealed DM on the wire.

    Attributes:
        epoch: Epoch number whose secret keys this frame.
        index: Zero-based message index within the epoch.
        nonce: 12-byte AES-GCM nonce.
        body: AES-256-GCM ciphertext+tag of the plaintext.
        kam: Wrapped epoch secret (``skchat.dm_ratchet.wrap_dm_epoch_secret``),
            present ONLY on the first frame of an epoch; ``None`` otherwise.
    """

    epoch: int
    index: int
    nonce: bytes
    body: bytes
    kam: Optional[bytes] = None


class DmSession:
    """Stateful 1:1 ratchet session for one peer (drives a :class:`DmRatchet`)."""

    def __init__(
        self,
        peer: str,
        *,
        rekey_msg_bound: int = DEFAULT_REKEY_MSG_BOUND,
        rekey_age_seconds: int = DEFAULT_REKEY_AGE_SECONDS,
    ) -> None:
        self.peer = peer
        self.rekey_msg_bound = rekey_msg_bound
        self.rekey_age_seconds = rekey_age_seconds
        self._ratchet: Optional[DmRatchet] = None
        self._epoch_secrets: dict[int, bytes] = {}

    # -- sender ---------------------------------------------------------------

    def _begin_epoch(self, epoch: int, peer_hybrid_pub: bytes) -> bytes:
        """Start a fresh epoch: new secret, set the outbound ratchet, return the KAM."""
        secret = new_epoch_secret()
        self._epoch_secrets[epoch] = secret
        self._ratchet = DmRatchet(
            epoch=epoch,
            epoch_secret=secret,
            rekey_msg_bound=self.rekey_msg_bound,
            rekey_age_seconds=self.rekey_age_seconds,
        )
        return wrap_dm_epoch_secret(secret, peer_hybrid_pub)

    def seal(self, plaintext: bytes, peer_hybrid_pub: bytes) -> SealedDmFrame:
        """Seal a plaintext to the peer, (re)keying as needed.

        Establishes epoch 0 on first use, or rolls to the next epoch once the
        current one hits its bound — the KAM rides on that epoch's first frame.
        """
        kam: Optional[bytes] = None
        if self._ratchet is None:
            kam = self._begin_epoch(0, peer_hybrid_pub)
        elif self._ratchet.should_rekey():
            kam = self._begin_epoch(self._ratchet.epoch + 1, peer_hybrid_pub)

        idx, key = self._ratchet.next_outbound_key()
        epoch = self._ratchet.epoch
        nonce = os.urandom(_FRAME_NONCE_LEN)
        body = AESGCM(key).encrypt(nonce, plaintext, _frame_aad(epoch, idx))
        return SealedDmFrame(epoch=epoch, index=idx, nonce=nonce, body=body, kam=kam)

    # -- receiver -------------------------------------------------------------

    def open(self, frame: SealedDmFrame, my_hybrid_priv: bytes) -> bytes:
        """Open a sealed frame, accepting its KAM if it carries a new epoch."""
        if frame.kam is not None and frame.epoch not in self._epoch_secrets:
            self._epoch_secrets[frame.epoch] = unwrap_dm_epoch_secret(
                frame.kam, my_hybrid_priv
            )
        secret = self._epoch_secrets.get(frame.epoch)
        if secret is None:
            raise DmRatchetError(
                f"no epoch secret for epoch {frame.epoch} (missing key-agreement message)"
            )
        key = derive_dm_message_key(secret, frame.epoch, frame.index)
        return AESGCM(key).decrypt(frame.nonce, frame.body, _frame_aad(frame.epoch, frame.index))

    # -- test/introspection ---------------------------------------------------

    def _epoch_secret_for_test(self, epoch: int) -> Optional[bytes]:
        """Return the stored secret for ``epoch`` (tests / introspection only)."""
        return self._epoch_secrets.get(epoch)
