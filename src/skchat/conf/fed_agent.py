"""Federated conf-agent join (C4): pull the AI agent into a conf room hosted on
a REMOTE instance's SFU.

The in-room agent today (``lumina-call.py``, run by ``/conf/{room}/invite-agent``)
self-mints a LOCAL LiveKit token over the loopback ``WEBUI_URL/livekit/token``
gate and joins THIS instance's SFU. That only works for rooms hosted here.

This module is the federated counterpart. To join a room hosted on box A from
box B, the agent must instead:

  1. **discover** which instance is the elected SFU focus for ``room``
     (:meth:`FederationDiscoveryClient.discover_and_elect`), or accept an
     explicit ``host`` auth_url (skip discovery),
  2. **mint a cross-realm token** at that host's conf authd via
     :func:`skchat.conf.fed_client.mint_remote_conf_token` (a capauth-signed
     FQID assertion → remote ``/conf/{room}/federated-token``), and
  3. **join the REMOTE SFU** with the returned ``{token, url}`` — reusing the
     EXISTING agent media stack (``lumina-call.py``) rather than duplicating it.

Step 3 is the key reuse: the agent script already knows how to connect to a
``url`` with a ``token`` and publish/subscribe media. The only gap is that it
self-mints; so for the federated path we hand it the PRE-MINTED token + SFU url
out-of-band (``SKCHAT_CONF_TOKEN`` / ``SKCHAT_CONF_URL`` env, with ``--token`` /
``--url`` CLI fallbacks). The script — in a separate repo, never modified here —
is taught to prefer those when present (and it harmlessly ignores them on an
older build, falling back to the local mint, which simply fails fast for a
remote room — no media-stack duplication either way).

Every external dependency (discovery, mint, process spawn) is injectable so the
whole flow is unit-testable with fakes — no relay, no network, no keys, no spawn.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("skchat.conf.fed_agent")


class FederatedAgentJoinError(Exception):
    """The federated agent-join could not resolve a host or mint a token."""


# Mint seam: (remote_auth_url, room, *, fqid) -> {token, url, role, identity, ...}
MintFn = Callable[..., dict]
# Discover seam: (room) -> object exposing ``.auth_url`` and ``.sfu_ws_url``.
DiscoverFn = Callable[[str], "object"]
# Spawn seam: (cmd, env) -> a process-ish object (records start in tests).
SpawnFn = Callable[[list, dict], "object"]


def _default_lumina_call_script() -> str:
    """Absolute path to lumina-call.py (override ``SKCHAT_LUMINA_CALL_SCRIPT``).

    Mirrors :mod:`skchat.conf.routes` so the local and federated invite paths
    spawn the SAME agent media stack.
    """
    default = str(
        Path.home()
        / "clawd"
        / "skcapstone-repos"
        / "lumina-creative"
        / "scripts"
        / "lumina-call.py"
    )
    return os.getenv("SKCHAT_LUMINA_CALL_SCRIPT", default)


def _default_agent_python() -> str:
    return os.getenv("SKCHAT_AGENT_PYTHON", str(Path.home() / ".skenv" / "bin" / "python"))


def _sanitize_unit(room: str) -> str:
    """Sanitize ``room`` into the alnum/dash slug used in a systemd unit name."""
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", room).strip("-")
    return slug or "room"


def fed_agent_unit(room: str) -> str:
    """The systemd ``--scope`` unit name for the FEDERATED conf-agent of ``room``.

    Distinct prefix (``lumina-fedconf-``) from the local ``lumina-conf-`` units
    so a federated join and a (hypothetical) local join of the same room id are
    independently startable/stoppable and never collide.
    """
    return f"lumina-fedconf-{_sanitize_unit(room)}"


def _default_discover(room: str):
    """Discover + elect the SFU focus for ``room`` via the Nostr relay(s)."""
    from skchat.spaces.federation.discovery import FederationDiscoveryClient

    relays = [r for r in os.getenv("SKCHAT_NOSTR_RELAYS", "").split(",") if r.strip()]
    return FederationDiscoveryClient(relays=relays).discover_and_elect(room)


def _default_mint(remote_auth_url: str, room: str, *, fqid: Optional[str] = None) -> dict:
    from skchat.conf.fed_client import mint_remote_conf_token

    return mint_remote_conf_token(remote_auth_url, room, fqid=fqid)


def _default_spawn(cmd: list, env: dict):
    """Spawn the agent as a supervised, resource-scoped systemd ``--scope`` unit.

    Fire-and-forget: we only confirm it started, mirroring
    ``conf.routes._agent_runner``. The pre-minted token + SFU url ride in on the
    child's environment (``env``) so they never appear in ``ps``/the unit args.
    """
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    return proc


def mint_federated_agent_token(
    room: str,
    *,
    host: Optional[str] = None,
    fqid: Optional[str] = None,
    discover: Optional[DiscoverFn] = None,
    mint: Optional[MintFn] = None,
) -> dict:
    """Resolve the remote host for ``room`` and mint a cross-realm conf token.

    If ``host`` (a remote auth_url / base URL) is given, discovery is skipped and
    the token is minted directly against it. Otherwise the elected SFU focus is
    discovered from the relay and its advertised ``auth_url`` is used.

    Returns the mint payload ``{token, url, role, identity, conf_id, room}`` —
    everything the agent needs to join the REMOTE SFU. The ``url`` is the remote
    SFU websocket url returned by the authd (falling back to the discovered
    ``sfu_ws_url`` if the authd omitted it).

    Raises:
        FederatedAgentJoinError: no host could be resolved / discovery failed.
        ConfAuthDenied / ConfFederationError: the remote authd rejected us.
    """
    _discover = discover or _default_discover
    _mint = mint or _default_mint

    auth_url = (host or "").strip()
    discovered_sfu = ""
    if not auth_url:
        try:
            elected = _discover(room)
        except Exception as exc:  # noqa: BLE001 - surface discovery failure cleanly
            raise FederatedAgentJoinError(
                f"discovery failed for room {room!r}: {exc}"
            ) from exc
        auth_url = (getattr(elected, "auth_url", "") or "").strip()
        discovered_sfu = (getattr(elected, "sfu_ws_url", "") or "").strip()
        if not auth_url:
            raise FederatedAgentJoinError(
                f"no auth_url discovered for room {room!r}"
            )
        logger.info(
            "federated agent-join: elected host for %s → %s (sfu=%s)",
            room,
            auth_url,
            discovered_sfu,
        )

    payload = dict(_mint(auth_url, room, fqid=fqid))
    # The authd's payload carries the SFU ws url under ``url``; if it didn't,
    # fall back to the focus descriptor's advertised ``sfu_ws_url``.
    if not (payload.get("url") or "").strip() and discovered_sfu:
        payload["url"] = discovered_sfu
    payload.setdefault("room", room)
    payload.setdefault("auth_url", auth_url)
    return payload


def federated_agent_join(
    room: str,
    *,
    host: Optional[str] = None,
    fqid: Optional[str] = None,
    greet: str = "Lumina here — joining the federated conference.",
    discover: Optional[DiscoverFn] = None,
    mint: Optional[MintFn] = None,
    spawn: Optional[SpawnFn] = None,
    lumina_call_script: Optional[str] = None,
    agent_python: Optional[str] = None,
) -> dict:
    """Join a REMOTE-hosted conf ``room`` as the AI agent.

    Discovers (or accepts ``host``), mints a cross-realm token, then spawns the
    EXISTING agent media stack (``lumina-call.py``) against the remote SFU —
    handing it the pre-minted ``{token, url}`` via environment so it joins the
    remote SFU instead of self-minting a local token.

    Returns ``{ok, unit, room, url, identity, role}`` describing the launched
    federated conf-agent. ``spawn`` is injected by tests so no process is ever
    started and no relay/authd is touched.

    Raises:
        FederatedAgentJoinError: host/token resolution failed (no spawn happens).
    """
    if len(greet) > 500:
        raise FederatedAgentJoinError("greeting too long (max 500 chars)")

    payload = mint_federated_agent_token(
        room, host=host, fqid=fqid, discover=discover, mint=mint
    )
    token = (payload.get("token") or "").strip()
    url = (payload.get("url") or "").strip()
    if not (token and url):
        raise FederatedAgentJoinError(
            f"mint returned an incomplete token/url for room {room!r}"
        )

    _spawn = spawn or _default_spawn
    script = lumina_call_script or _default_lumina_call_script()
    python = agent_python or _default_agent_python()
    unit = fed_agent_unit(room)

    # Pre-minted creds ride in on the environment (never in argv / ps output).
    # ``--token`` / ``--url`` are also passed as a CLI fallback for an agent
    # build that reads args rather than env; both name the SAME remote SFU.
    child_env = dict(os.environ)
    child_env["SKCHAT_CONF_TOKEN"] = token
    child_env["SKCHAT_CONF_URL"] = url

    cmd = [
        "systemd-run",
        "--user",
        "--scope",
        f"--unit={unit}",
        "--property=MemoryMax=2G",
        "--property=CPUQuota=200%",
        python,
        script,
        "--room",
        room,
        "--say",
        greet,
        "--url",
        url,
        "--token",
        token,
        "--stay",
    ]

    logger.info(
        "federated agent-join: spawning %s for room=%s url=%s identity=%s",
        unit,
        room,
        url,
        payload.get("identity", ""),
    )
    _spawn(cmd, child_env)

    return {
        "ok": True,
        "unit": unit,
        "room": room,
        "url": url,
        "identity": payload.get("identity", ""),
        "role": payload.get("role", ""),
    }
