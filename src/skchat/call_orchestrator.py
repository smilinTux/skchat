"""Layered call orchestration (sub-project C): P2P first, SFU fallback.

"If you need one, get two" at the transport layer: attempt the sovereign direct
P2P link (sub-project B); if it can't establish within a timeout (NAT/firewall, no
ICE path), fall back to the LiveKit SFU room from sub-project A. Both transports
land in the *same deterministic room* (``call_session.derive_room``), so the
fallback is seamless — the peers were already going to meet there.

The Talk-compat shim (a federated/Nextcloud-Talk surface) is the other half of C
and is deferred per the Talk-fit decision; this module is the P2P↔SFU layering.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("skchat.call_orchestrator")

DEFAULT_P2P_TIMEOUT = 8.0


# -- monkeypatchable seams ----------------------------------------------------
async def _attempt_p2p(fqid: str, timeout: float):
    """Dial P2P and wait for the data channel to open. Raises on failure/timeout."""
    from .p2p_calls import get_manager

    session = await get_manager().call(fqid)
    await asyncio.wait_for(session.wait_open(timeout=timeout), timeout=timeout + 1)
    return session


def _livekit_fallback(fqid: str) -> dict:
    """Prepare the LiveKit SFU session for the same deterministic room (sub-project A)."""
    from .call_routes import _prepare_call

    ctx = _prepare_call(fqid)
    return {k: ctx[k] for k in ("room", "token", "livekit_url", "peer_fqid", "identity")}


def _resolve(peer: str) -> str:
    from .p2p_calls import resolve_fqid

    return resolve_fqid(peer)


# -- orchestration ------------------------------------------------------------
async def connect_with_fallback(peer: str, *, p2p_timeout: float = DEFAULT_P2P_TIMEOUT) -> dict:
    """Connect to ``peer``, preferring direct P2P, falling back to the LiveKit room.

    Returns:
        ``{"transport": "p2p", ...}`` if the direct link opened, else
        ``{"transport": "livekit", room, token, livekit_url, ...}`` for the SFU room.
    """
    fqid = _resolve(peer)

    try:
        await _attempt_p2p(fqid, p2p_timeout)
        logger.info("call: P2P established with %s", fqid)
        return {"transport": "p2p", "peer_fqid": fqid, "status": "connected"}
    except Exception as exc:  # noqa: BLE001 — any P2P failure → SFU fallback
        logger.info("call: P2P failed for %s (%s) — falling back to LiveKit SFU", fqid, exc)

    ctx = _livekit_fallback(fqid)
    return {"transport": "livekit", "status": "fallback", **ctx}
