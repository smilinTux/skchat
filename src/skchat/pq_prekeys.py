"""Prekey store + Lumina's own hybrid keypair (PQC-MIGRATION Q5, app-side wiring).

This is the daemon/webui half of Q5: it lets the Flutter app (chef) and the
daemon (Lumina) exchange **PQXDH-style hybrid-KEM prekeys** so DMs go hybrid
post-quantum end-to-end.

Two responsibilities:

1. **Peer prekey store.** The app publishes its device prekey bundle via
   ``POST /api/v1/prekey``; the daemon persists it here (keyed by short name) and
   serves it back via ``GET /api/v1/prekey/{peer}``. Lumina's send path looks up
   the operator's stored bundle to seal her reply.

2. **Lumina's own hybrid keypair.** Generated once (via :mod:`skcomms.pqkem`),
   persisted 0600, and exposed both as a published bundle (so the app can seal
   to her) and as the private key (so she can open hybrid DMs addressed to her).

Storage: ``~/.skchat/pqc/`` —
   * ``peers/<short>.json``   — published peer bundles (JSON)
   * ``lumina_hybrid.key``    — Lumina's 2432-byte hybrid private key (hex, 0600)
   * ``lumina_hybrid.pub``    — Lumina's 1216-byte hybrid public key (hex)

Honesty: if liboqs is unavailable, Lumina simply publishes no hybrid prekey
(``available() is False``) and every conversation stays classical — the same
negotiated-downgrade contract the rest of Q3 uses. Never a silent failure.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HYBRID_SUITE = "x25519-mlkem768"
CLASSICAL_SUITE = "x25519-pgp-wrap-v1"


def _pqc_dir() -> Path:
    home = Path(os.environ.get("SKCHAT_HOME", Path.home() / ".skchat"))
    d = home / "pqc"
    (d / "peers").mkdir(parents=True, exist_ok=True)
    return d


def _short(uri: str) -> str:
    s = uri[len("capauth:") :] if uri.startswith("capauth:") else uri
    return s.split("@")[0]


def _current_agent() -> str:
    """The local resident agent short name (SKAGENT, fallbacks, default lumina)."""
    return (
        os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or os.environ.get("SKMEMORY_AGENT")
        or "lumina"
    ).split("@")[0]


# --------------------------------------------------------------------------- #
# Peer prekey store
# --------------------------------------------------------------------------- #


def store_peer_bundle(peer: str, bundle: dict) -> None:
    """Persist a published peer prekey bundle (keyed by short name)."""
    path = _pqc_dir() / "peers" / f"{_short(peer)}.json"
    # Normalise — only keep the contract fields.
    safe = {
        "suite": bundle.get("suite", CLASSICAL_SUITE),
        "hybrid_public_hex": bundle.get("hybrid_public_hex", "") or "",
        "signature": bundle.get("signature"),
        "key_id": bundle.get("key_id"),
        "device_id": bundle.get("device_id"),
    }
    path.write_text(json.dumps(safe, indent=2))


def load_peer_bundle(peer: str) -> Optional[dict]:
    """Return a stored peer bundle (or None if the peer never published one)."""
    path = _pqc_dir() / "peers" / f"{_short(peer)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        logger.warning("corrupt peer prekey for %s", peer, exc_info=True)
        return None


def peer_is_hybrid(peer: str) -> bool:
    b = load_peer_bundle(peer)
    return bool(b and b.get("suite") == HYBRID_SUITE and b.get("hybrid_public_hex"))


# --------------------------------------------------------------------------- #
# Lumina's own hybrid keypair
# --------------------------------------------------------------------------- #


def available() -> bool:
    """Whether the PQ backend (liboqs via skcomms.pqkem) is reachable."""
    try:
        from skcomms import pqkem

        return pqkem.is_available()
    except Exception:
        return False


def ensure_agent_keypair(agent: Optional[str] = None) -> Optional[tuple[bytes, bytes]]:
    """Load-or-generate the resident agent's hybrid keypair.

    PQC cut-over: every resident agent (not just Lumina) publishes a hybrid
    prekey on startup so DMs to it negotiate hybrid by default. The key is
    persisted 0600 at ``~/.skchat/pqc/<agent>_hybrid.key`` / ``.pub``.

    Lumina keeps her historical ``lumina_hybrid.*`` filenames (so existing
    on-disk keys and the published bundle stay byte-identical); other agents use
    ``<agent>_hybrid.*``.

    Returns ``(public, private)`` or ``None`` if no PQ backend is available
    (honest classical fallback — never a silent failure).
    """
    agent = (agent or _current_agent()).split("@")[0]
    if not available():
        return None
    d = _pqc_dir()
    priv_path = d / f"{agent}_hybrid.key"
    pub_path = d / f"{agent}_hybrid.pub"
    if priv_path.exists() and pub_path.exists():
        try:
            return (
                bytes.fromhex(pub_path.read_text().strip()),
                bytes.fromhex(priv_path.read_text().strip()),
            )
        except Exception:
            logger.warning("corrupt %s hybrid key — regenerating", agent, exc_info=True)
    try:
        from skcomms import pqkem

        kp = pqkem.hybrid_keypair()
    except Exception:
        logger.exception("%s hybrid keypair generation failed", agent)
        return None
    pub_path.write_text(kp.public_key.hex())
    priv_path.write_text(kp.private_key.hex())
    try:
        os.chmod(priv_path, 0o600)
    except OSError:
        pass
    return (kp.public_key, kp.private_key)


def agent_private(agent: Optional[str] = None) -> Optional[bytes]:
    kp = ensure_agent_keypair(agent)
    return kp[1] if kp else None


def agent_public(agent: Optional[str] = None) -> Optional[bytes]:
    kp = ensure_agent_keypair(agent)
    return kp[0] if kp else None


def agent_bundle(agent: Optional[str] = None) -> dict:
    """The resident agent's published prekey bundle (hybrid if available)."""
    agent = (agent or _current_agent()).split("@")[0]
    kp = ensure_agent_keypair(agent)
    if not kp:
        return {"suite": CLASSICAL_SUITE, "hybrid_public_hex": ""}
    pub, _ = kp
    return {
        "suite": HYBRID_SUITE,
        "hybrid_public_hex": pub.hex(),
        # Signature stays classical (Phase 2 / Q7).
        "signature": None,
        "key_id": pub.hex()[:16],
        "device_id": f"{agent}-daemon",
    }


def publish_self_prekey(agent: Optional[str] = None) -> dict:
    """Generate (if needed) + return the resident agent's prekey bundle.

    Startup hook: a daemon calls this once on boot so the agent's hybrid prekey
    exists and is serveable (via ``GET /api/v1/prekey/<agent>``). Returns the
    published bundle (``suite``/``hybrid_public_hex``/…). When liboqs is absent
    the bundle is classical-only — honest, never raised.
    """
    agent = (agent or _current_agent()).split("@")[0]
    bundle = agent_bundle(agent)
    # Register the agent in the SHARED peer store so co-resident agents resolve it
    # via load_peer_bundle() (RFC-0001 P1: the local-fleet prekey "exchange").
    store_peer_bundle(agent, bundle)
    if bundle.get("suite") == HYBRID_SUITE:
        logger.info("PQC: published hybrid prekey for resident agent %s", agent)
    else:
        logger.info(
            "PQC: no hybrid backend — agent %s publishes a classical prekey "
            "(DMs to it stay classical until liboqs is available)",
            agent,
        )
    return bundle


def sync_fleet_prekeys() -> dict[str, str]:
    """Publish every co-resident agent's prekey into the shared peer store.

    Scans the PQ dir for ``<agent>_hybrid.pub`` keypairs and registers each one
    (idempotent) so all co-resident agents can resolve each other and DMs negotiate
    the Level-3 ratchet. Returns ``{agent: suite}``.
    """
    published: dict[str, str] = {}
    for pub in sorted(_pqc_dir().glob("*_hybrid.pub")):
        agent = pub.name[: -len("_hybrid.pub")]
        if not agent:
            continue
        bundle = publish_self_prekey(agent)
        published[agent] = bundle.get("suite", CLASSICAL_SUITE)
    return published


# --------------------------------------------------------------------------- #
# Lumina back-compat aliases (the daemon_proxy + webui call these by name).
# --------------------------------------------------------------------------- #


def ensure_lumina_keypair() -> Optional[tuple[bytes, bytes]]:
    """Back-compat alias for :func:`ensure_agent_keypair` pinned to ``lumina``."""
    return ensure_agent_keypair("lumina")


def lumina_private() -> Optional[bytes]:
    return agent_private("lumina")


def lumina_bundle() -> dict:
    """Lumina's published prekey bundle (hybrid if available, else classical)."""
    return agent_bundle("lumina")


# --------------------------------------------------------------------------- #
# Group-create helper: collect hybrid prekeys for a set of members.
# --------------------------------------------------------------------------- #


def hybrid_pub_hex_for(identity_uri: str, *, self_agent: Optional[str] = None) -> str:
    """Best-effort hybrid public-key hex for ``identity_uri`` (or "").

    Resolution order:
      1. If the short name is the resident agent itself → its own public key.
      2. The published peer bundle in ``~/.skchat/pqc/peers/<short>.json``.
    Returns "" when no hybrid key is known (the member then falls back to the
    classical wrap and is flagged in the group self-report — never locked out).
    """
    short = _short(identity_uri)
    me = (self_agent or _current_agent()).split("@")[0]
    if short == me:
        pub = agent_public(me)
        return pub.hex() if pub else ""
    bundle = load_peer_bundle(short)
    if bundle and bundle.get("suite") == HYBRID_SUITE and bundle.get("hybrid_public_hex"):
        return str(bundle["hybrid_public_hex"])
    return ""


def collect_member_hybrid_keys(
    identities: list[str], *, self_agent: Optional[str] = None
) -> dict[str, str]:
    """Map every ``identity_uri -> hex(hybrid pub)`` that we can resolve.

    Members with no known hybrid key are omitted (they fall back classically).
    Used by the group-create paths so a new group is hybrid-from-epoch-1 for the
    members that have prekeys, without locking out classical-only peers.
    """
    out: dict[str, str] = {}
    for uri in identities:
        pub_hex = hybrid_pub_hex_for(uri, self_agent=self_agent)
        if pub_hex:
            out[uri] = pub_hex
    return out
