"""skchat glue for the sovereign P2P stack (sub-project B).

A process-wide :class:`~skcomms.transports.p2p_manager.P2PSessionManager` (one per
agent) driven by the signed mailbox signaling. Exposes the verbs the MCP tools +
webui call: ``p2p_call`` (dial a paired peer), ``p2p_listen`` (start auto-answering
incoming offers), ``p2p_status``, ``p2p_send``. Long-lived sessions live in the
manager; the route loop runs in the host process's asyncio loop (the MCP server /
webui), so sessions persist across tool calls.

Sovereign by default (MailboxSignaling); a broker fast path can be layered later via
``select_signaling``.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("skchat.p2p_calls")

# Process-wide manager (lazy). Tests may set this directly.
_manager = None
_incoming: list[str] = []


# -- monkeypatchable seams ----------------------------------------------------
def _list_peers() -> dict:
    from skcomms.peers import list_peers
    return list_peers()


def _self_agent() -> Optional[str]:
    from capauth import resolve_agent_identity
    return resolve_agent_identity().agent


def resolve_fqid(peer: str) -> str:
    """Resolve a peer arg (FQID or bare name) to a single paired FQID.

    Raises:
        ValueError: if the peer is not paired or the bare name is ambiguous.
    """
    peers = _list_peers()
    if peer in peers:
        return peer
    matches = [fqid for fqid in peers if fqid.split("@", 1)[0] == peer]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"ambiguous bare name {peer!r}: {matches}; use full FQID")
    raise ValueError(f"peer not paired: {peer}")


def _on_ring(peer_fqid: str, session) -> None:
    if peer_fqid not in _incoming:
        _incoming.append(peer_fqid)
    logger.info("p2p: incoming session with %s", peer_fqid)


def get_manager():
    """Return the process-wide P2PSessionManager (created on first use)."""
    global _manager
    if _manager is None:
        from skcomms.transports.p2p_manager import P2PSessionManager
        from skcomms.transports.signaling_mailbox import MailboxSignaling

        _manager = P2PSessionManager(
            signaling=MailboxSignaling(_self_agent()),
            on_session=_on_ring,
        )
    return _manager


# -- verbs --------------------------------------------------------------------
async def p2p_call(peer: str) -> dict:
    """Dial a paired peer over the sovereign P2P stack (we are the offerer)."""
    fqid = resolve_fqid(peer)
    await get_manager().call(fqid)
    return {"peer_fqid": fqid, "status": "calling", "transport": "p2p"}


async def p2p_listen() -> dict:
    """Start the route loop so incoming offers are auto-answered."""
    await get_manager().start()
    return {"listening": True, "auto_answer": True}


def p2p_status() -> dict:
    """Active P2P sessions + any surfaced incoming peers."""
    mgr = get_manager()
    active = []
    for peer in mgr.active():
        session = mgr.get(peer)
        active.append({"peer": peer, "open": bool(session and session.is_open)})
    return {"active": active, "incoming": list(_incoming)}


async def p2p_send(peer: str, text: str) -> dict:
    """Send a string over an open P2P data channel to ``peer``."""
    fqid = resolve_fqid(peer)
    session = get_manager().get(fqid)
    if session is None:
        raise ValueError(f"no active P2P session with {fqid}")
    session.send(text)
    return {"sent": True, "peer_fqid": fqid}
