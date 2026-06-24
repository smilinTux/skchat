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


def ensure_lumina_keypair() -> Optional[tuple[bytes, bytes]]:
    """Load-or-generate Lumina's hybrid keypair. Returns ``(public, private)``
    or None if no PQ backend is available.
    """
    if not available():
        return None
    d = _pqc_dir()
    priv_path = d / "lumina_hybrid.key"
    pub_path = d / "lumina_hybrid.pub"
    if priv_path.exists() and pub_path.exists():
        try:
            return (
                bytes.fromhex(pub_path.read_text().strip()),
                bytes.fromhex(priv_path.read_text().strip()),
            )
        except Exception:
            logger.warning("corrupt Lumina hybrid key — regenerating", exc_info=True)
    try:
        from skcomms import pqkem

        kp = pqkem.hybrid_keypair()
    except Exception:
        logger.exception("Lumina hybrid keypair generation failed")
        return None
    pub_path.write_text(kp.public_key.hex())
    priv_path.write_text(kp.private_key.hex())
    try:
        os.chmod(priv_path, 0o600)
    except OSError:
        pass
    return (kp.public_key, kp.private_key)


def lumina_private() -> Optional[bytes]:
    kp = ensure_lumina_keypair()
    return kp[1] if kp else None


def lumina_bundle() -> dict:
    """Lumina's published prekey bundle (hybrid if available, else classical)."""
    kp = ensure_lumina_keypair()
    if not kp:
        return {"suite": CLASSICAL_SUITE, "hybrid_public_hex": ""}
    pub, _ = kp
    return {
        "suite": HYBRID_SUITE,
        "hybrid_public_hex": pub.hex(),
        # Signature stays classical (Phase 2 / Q7).
        "signature": None,
        "key_id": pub.hex()[:16],
        "device_id": "lumina-daemon",
    }
