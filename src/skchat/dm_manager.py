"""DmRatchetManager — live-path orchestration for the 1:1 DM ratchet (RFC-0001 P1).

The thin layer the transport calls. It ties together the four pieces the ratchet
needs and exposes exactly two operations — :meth:`seal` (outbound) and :meth:`open`
(inbound) — each with **honest fallback**:

* the agent's own hybrid keypair (from :mod:`skchat.pq_prekeys`),
* peer prekey resolution (a peer with no published hybrid prekey → no ratchet),
* the sealed :class:`skchat.dm_store.DmSessionStore` (epoch secrets encrypted at rest),
* :class:`skchat.crypto.ChatCrypto`'s ``encrypt/decrypt_message_ratchet`` methods.

Fallback is the whole safety story: if the PQ backend is absent, or the peer has no
hybrid prekey, :meth:`seal` returns the message **untouched** so the caller takes the
existing classical/hybrid-one-shot path — classical conversations are byte-for-byte
unchanged. :meth:`open` only handles ``pqdr1:`` ratchet bodies; anything else passes
straight through.

The constructor is fully injectable (for tests); :meth:`for_agent` wires the real
:mod:`skchat.pq_prekeys` and derives the at-rest store key from the agent's hybrid
private key, so it inherits the keypair's lifecycle and needs no new persisted secret.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Union

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from skchat.crypto import ChatCrypto
from skchat.dm_session import DmSession
from skchat.dm_store import DmSessionStore
from skchat.models import ChatMessage

_HYBRID_SUITE = "x25519-mlkem768"
_RATCHET_CAP = "pqdr1"
_STORE_KEY_INFO = b"skchat/dm-store-key/v1"


def _derive_store_key(agent_private: bytes) -> bytes:
    """Derive the 32-byte at-rest store-seal key from the agent's hybrid private key."""
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"", info=_STORE_KEY_INFO
    ).derive(agent_private)


class DmRatchetManager:
    """Orchestrates the 1:1 ratchet on the live message path (seal / open)."""

    def __init__(
        self,
        crypto: ChatCrypto,
        *,
        agent_public: Optional[bytes],
        agent_private: Optional[bytes],
        peer_pub_resolver: Callable[[str], Optional[bytes]],
        store: DmSessionStore,
        store_key: bytes,
    ) -> None:
        self._crypto = crypto
        self._pub = agent_public
        self._priv = agent_private
        self._resolve = peer_pub_resolver
        self._store = store
        self._store_key = store_key

    # -- capability gates -----------------------------------------------------

    def can_ratchet(self, peer: str) -> bool:
        """Whether an outbound DM to ``peer`` can use the ratchet (have keys + prekey)."""
        return self._priv is not None and self._resolve(peer) is not None

    def can_open(self, message: ChatMessage) -> bool:
        """Whether ``message`` is a ratchet body this agent can open."""
        return self._priv is not None and ChatCrypto.is_ratchet_message(message)

    # -- session lifecycle ----------------------------------------------------

    def _session(self, peer: str) -> DmSession:
        existing = self._store.load(peer, self._store_key)
        return existing if existing is not None else DmSession(peer=peer)

    # -- outbound / inbound ---------------------------------------------------

    def seal(self, message: ChatMessage) -> ChatMessage:
        """Ratchet-seal an outbound DM, or return it untouched for classical fallback."""
        peer = message.recipient
        peer_pub = self._resolve(peer) if self._priv is not None else None
        if peer_pub is None:
            return message  # no ratchet — caller takes the classical/hybrid path
        session = self._session(peer)
        sealed = self._crypto.encrypt_message_ratchet(message, session, peer_pub)
        self._store.save(session, self._store_key)
        return sealed

    def open(self, message: ChatMessage) -> ChatMessage:
        """Open a ratchet-sealed inbound DM, or return it untouched if not one."""
        if not self.can_open(message):
            return message
        peer = message.sender
        session = self._session(peer)
        opened = self._crypto.decrypt_message_ratchet(message, session, self._priv)
        self._store.save(session, self._store_key)
        return opened

    # -- factory --------------------------------------------------------------

    @classmethod
    def for_agent(
        cls,
        crypto: ChatCrypto,
        agent: str,
        store_dir: Union[str, Path],
        *,
        prekeys=None,
    ) -> Optional["DmRatchetManager"]:
        """Wire the real prekey store + the agent's hybrid keypair.

        Returns ``None`` when no PQ backend is available (the agent has no hybrid
        keypair) — the caller then stays on the classical/hybrid-one-shot path.
        """
        if prekeys is None:
            from skchat import pq_prekeys as prekeys  # local import (optional dep)

        kp = prekeys.ensure_agent_keypair(agent)
        if kp is None:
            return None
        pub, priv = kp

        def _resolve(peer: str) -> Optional[bytes]:
            bundle = prekeys.load_peer_bundle(peer)
            # Capability gate: only ratchet with peers that advertise the pqdr1
            # wire format — an app/older client with a hybrid prekey but no
            # ``ratchet`` capability stays on the classical/one-shot path so it
            # never receives an undecryptable frame (RFC-0001 downgrade protection).
            if (
                bundle
                and bundle.get("suite") == _HYBRID_SUITE
                and bundle.get("hybrid_public_hex")
                and bundle.get("ratchet") == _RATCHET_CAP
            ):
                try:
                    return bytes.fromhex(bundle["hybrid_public_hex"])
                except ValueError:
                    return None
            return None

        store_dir = Path(store_dir)
        store_dir.mkdir(parents=True, exist_ok=True)
        store = DmSessionStore(store_dir / "dm_sessions.db")
        return cls(
            crypto,
            agent_public=pub,
            agent_private=priv,
            peer_pub_resolver=_resolve,
            store=store,
            store_key=_derive_store_key(priv),
        )
