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

import base64
import os
import struct
from dataclasses import dataclass
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

#: Wire marker for a sealed DM *ratchet* frame stored in ``ChatMessage.content``.
#: Mirrors the hybrid-DM ``pqdm1:`` token shape (``skchat.crypto.PQDM_SCHEME``):
#: classical PGP starts with ``-----BEGIN PGP``, hybrid one-shot with ``pqdm1:``,
#: and a ratchet frame with this prefix — all three coexist in the same field.
PQDR_SCHEME = "pqdr1:"


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

    def to_token(self) -> str:
        """Serialize to the ``pqdr1:`` wire token (``PQDR_SCHEME + base64(binary)``).

        Explicit length-prefixed big-endian binary, so the form is self-describing
        and the round-trip is exact — including the ``kam=None`` vs ``kam=present``
        distinction (a one-byte presence flag separates "absent" from "empty")::

            epoch(u64) || index(u64)
              || nonce_len(u32) || nonce
              || body_len(u32)  || body
              || kam_flag(u8)   || [kam_len(u32) || kam]
        """
        parts = [
            struct.pack(
                ">QQ",
                self.epoch & 0xFFFFFFFFFFFFFFFF,
                self.index & 0xFFFFFFFFFFFFFFFF,
            ),
            struct.pack(">I", len(self.nonce)),
            self.nonce,
            struct.pack(">I", len(self.body)),
            self.body,
        ]
        if self.kam is None:
            parts.append(struct.pack(">B", 0))
        else:
            parts.append(struct.pack(">B", 1))
            parts.append(struct.pack(">I", len(self.kam)))
            parts.append(self.kam)
        blob = b"".join(parts)
        return PQDR_SCHEME + base64.b64encode(blob).decode("ascii")

    @classmethod
    def from_token(cls, token: str) -> "SealedDmFrame":
        """Parse a ``pqdr1:`` token back into a :class:`SealedDmFrame`.

        Raises:
            ValueError: if the token is not ``pqdr1:``-schemed, not valid base64,
                or the binary is truncated / malformed (never a crash on bad input).
        """
        if not isinstance(token, str) or not token.startswith(PQDR_SCHEME):
            raise ValueError(f"not a {PQDR_SCHEME!r} ratchet token")
        try:
            blob = base64.b64decode(token[len(PQDR_SCHEME) :], validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError(f"invalid base64 in ratchet token: {exc}") from exc

        view = memoryview(blob)
        off = 0

        def _take(n: int) -> bytes:
            nonlocal off
            if off + n > len(view):
                raise ValueError("truncated ratchet frame")
            chunk = bytes(view[off : off + n])
            off += n
            return chunk

        epoch, index = struct.unpack(">QQ", _take(16))
        (nonce_len,) = struct.unpack(">I", _take(4))
        nonce = _take(nonce_len)
        (body_len,) = struct.unpack(">I", _take(4))
        body = _take(body_len)
        (kam_flag,) = struct.unpack(">B", _take(1))
        if kam_flag == 0:
            kam: Optional[bytes] = None
        elif kam_flag == 1:
            (kam_len,) = struct.unpack(">I", _take(4))
            kam = _take(kam_len)
        else:
            raise ValueError(f"invalid kam presence flag: {kam_flag}")
        if off != len(view):
            raise ValueError("trailing bytes after ratchet frame")
        return cls(epoch=epoch, index=index, nonce=nonce, body=body, kam=kam)


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

    # -- persistence ----------------------------------------------------------

    def snapshot(self) -> dict:
        """Capture the full ratchet state as a JSON-safe dict (epoch secrets hex).

        The returned dict carries **key material** (the epoch secrets) — callers
        MUST seal it at rest (see :class:`skchat.dm_store.DmSessionStore`), never
        persist it in the clear.
        """
        r = self._ratchet
        return {
            "v": 1,
            "peer": self.peer,
            "rekey_msg_bound": self.rekey_msg_bound,
            "rekey_age_seconds": self.rekey_age_seconds,
            "epoch_secrets": {str(e): s.hex() for e, s in self._epoch_secrets.items()},
            "ratchet": None
            if r is None
            else {
                "epoch": r.epoch,
                "message_index": r.message_index,
                "epoch_started_at": r.epoch_started_at,
            },
        }

    @classmethod
    def restore(cls, snap: dict) -> "DmSession":
        """Rebuild a session from :meth:`snapshot` — same secrets, same next index."""
        s = cls(
            peer=snap["peer"],
            rekey_msg_bound=snap["rekey_msg_bound"],
            rekey_age_seconds=snap["rekey_age_seconds"],
        )
        s._epoch_secrets = {int(e): bytes.fromhex(h) for e, h in snap["epoch_secrets"].items()}
        rt = snap.get("ratchet")
        if rt is not None:
            s._ratchet = DmRatchet(
                epoch=rt["epoch"],
                epoch_secret=s._epoch_secrets[rt["epoch"]],
                message_index=rt["message_index"],
                rekey_msg_bound=s.rekey_msg_bound,
                rekey_age_seconds=s.rekey_age_seconds,
                epoch_started_at=rt["epoch_started_at"],
            )
        return s

    # -- test/introspection ---------------------------------------------------

    def _epoch_secret_for_test(self, epoch: int) -> Optional[bytes]:
        """Return the stored secret for ``epoch`` (tests / introspection only)."""
        return self._epoch_secrets.get(epoch)
