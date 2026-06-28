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

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Union

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from skchat.crypto import ChatCrypto
from skchat.dm_session import DmSession
from skchat.dm_store import DmSessionStore
from skchat.models import ChatMessage

logger = logging.getLogger(__name__)

_HYBRID_SUITE = "x25519-mlkem768"
_RATCHET_CAP = "pqdr1"
_STORE_KEY_INFO = b"skchat/dm-store-key/v1"


class AuthMode(str, Enum):
    """RFC-0001 §2.1 identity mode for a DM ratchet session.

    The KEM, the ratchet, and the wire format are **byte-identical** in both
    modes — only how (or whether) the session is *attributed* changes. The mode
    flips at session establishment; the per-message ratchet stays signature-free
    in both, so content deniability survives even in SOVEREIGN mode.
    """

    #: Deniable / no-DID (Chef's default). The resolved peer prekey is ratcheted
    #: as-is — nothing is verified, nothing is signed, no identity binding.
    ANONYMOUS = "anon-v1"
    #: Attributable. The peer's prekey bundle MUST carry a valid identity
    #: signature (verified before ratcheting); a sovereign session refuses an
    #: unsigned/invalid/wrong-identity bundle rather than silently downgrading.
    SOVEREIGN = "sovereign-v1"


def _mode_from_env(default: AuthMode = AuthMode.ANONYMOUS) -> AuthMode:
    """Resolve the per-deployment default mode from ``SKCHAT_DM_AUTH_MODE``.

    Honest default: ANONYMOUS unless the deployment explicitly opts every new
    conversation into sovereign. Anything unrecognised falls back to ANONYMOUS
    (never a silent upgrade).
    """
    raw = (os.environ.get("SKCHAT_DM_AUTH_MODE") or "").strip().lower()
    if raw in ("sovereign", "sovereign-v1", "did"):
        return AuthMode.SOVEREIGN
    if raw in ("anon", "anonymous", "anon-v1"):
        return AuthMode.ANONYMOUS
    return default


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
        mode: AuthMode = AuthMode.ANONYMOUS,
        peer_bundle_resolver: Optional[Callable[[str], Optional[dict]]] = None,
        peer_identity_resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self._crypto = crypto
        self._pub = agent_public
        self._priv = agent_private
        self._resolve = peer_pub_resolver
        self._store = store
        self._store_key = store_key
        #: RFC-0001 §2.1 auth mode. ANONYMOUS (default) ratchets the resolved
        #: prekey as-is; SOVEREIGN verifies a signed bundle before ratcheting.
        self.mode = mode
        #: SOVEREIGN-only hooks — the full peer bundle (for the signature) and
        #: the peer's identity public-key armor (to verify it against).
        self._resolve_bundle = peer_bundle_resolver
        self._resolve_identity = peer_identity_resolver

    # -- peer resolution (mode-aware) -----------------------------------------

    def _resolve_peer_pub(self, peer: str) -> Optional[bytes]:
        """The hybrid prekey to ratchet to ``peer``, gated by the auth mode.

        ANONYMOUS → the bare resolved pub (current behaviour, unverified).
        SOVEREIGN → verify the peer's signed bundle against its claimed identity
        first; on any failure (missing resolvers, unsigned, tampered, wrong
        identity) return ``None`` so the caller stays classical — a sovereign
        session **never** silently downgrades to an unattested ratchet.
        """
        if self.mode is not AuthMode.SOVEREIGN:
            return self._resolve(peer)
        return self._resolve_peer_pub_sovereign(peer)

    def _resolve_peer_pub_sovereign(self, peer: str) -> Optional[bytes]:
        if self._resolve_bundle is None or self._resolve_identity is None:
            logger.debug("sovereign DM to %s refused: no bundle/identity resolver", peer)
            return None
        bundle = self._resolve_bundle(peer)
        identity = self._resolve_identity(peer)
        if not bundle or not identity:
            return None
        # Local import keeps prekey_sig (pgpy) off the import path for anon-only
        # deployments and avoids any import cycle.
        from skchat.prekey_sig import verify_prekey_bundle

        if not verify_prekey_bundle(bundle, identity):
            logger.info("sovereign DM to %s refused: prekey bundle failed verification", peer)
            return None
        pub_hex = bundle.get("hybrid_public_hex")
        if not pub_hex:
            return None
        try:
            return bytes.fromhex(pub_hex)
        except ValueError:
            return None

    # -- capability gates -----------------------------------------------------

    def can_ratchet(self, peer: str) -> bool:
        """Whether an outbound DM to ``peer`` can use the ratchet (have keys + prekey).

        In SOVEREIGN mode this is also gated on the peer's prekey bundle carrying
        a valid identity signature.
        """
        return self._priv is not None and self._resolve_peer_pub(peer) is not None

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
        peer_pub = self._resolve_peer_pub(peer) if self._priv is not None else None
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
        mode: Optional[AuthMode] = None,
        peer_identity_resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> Optional["DmRatchetManager"]:
        """Wire the real prekey store + the agent's hybrid keypair.

        Returns ``None`` when no PQ backend is available (the agent has no hybrid
        keypair) — the caller then stays on the classical/hybrid-one-shot path.

        ``mode`` selects the RFC-0001 §2.1 auth mode; when ``None`` it follows the
        per-deployment default (``SKCHAT_DM_AUTH_MODE``, else ANONYMOUS) so the
        live path is unchanged unless a deployment opts in. In SOVEREIGN mode the
        peer's bundle is verified against an identity public-key armor obtained
        from ``peer_identity_resolver`` (a thin capauth hook the caller supplies);
        without one, sovereign DMs fail closed (no ratchet) — never a silent
        downgrade.
        """
        if prekeys is None:
            from skchat import pq_prekeys as prekeys  # local import (optional dep)

        mode = mode if mode is not None else _mode_from_env()

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
        # Agent-namespaced session DB so multiple agents that share one SKCHAT_HOME
        # (e.g. opus + jarvis on .41) don't collide in the peer-keyed dm_sessions
        # table (DmSessionStore is `dm_sessions(peer PRIMARY KEY)`). Per-agent keypairs
        # and prekeys are already per-agent-named, so only the session DB needed this.
        store = DmSessionStore(store_dir / f"dm_sessions_{agent}.db")
        return cls(
            crypto,
            agent_public=pub,
            agent_private=priv,
            peer_pub_resolver=_resolve,
            store=store,
            store_key=_derive_store_key(priv),
            mode=mode,
            # SOVEREIGN verification reads the full stored bundle (for its
            # signature) and the peer identity armor from the caller's hook.
            peer_bundle_resolver=prekeys.load_peer_bundle,
            peer_identity_resolver=peer_identity_resolver,
        )

    def signed_self_bundle(self, bundle: dict) -> dict:
        """Sign ``bundle`` with this manager's identity key for SOVEREIGN publishing.

        Thin helper over :func:`skchat.prekey_sig.sign_prekey_bundle` so a daemon
        publishing in SOVEREIGN mode advertises an attributable (signed) prekey.
        ANONYMOUS deployments simply never call this — the bundle stays unsigned
        and deniable. Additive: it does not mutate any store or the live path.
        """
        from skchat.prekey_sig import sign_prekey_bundle

        return sign_prekey_bundle(self._crypto, bundle)
