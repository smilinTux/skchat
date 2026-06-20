"""Federation observability surface (C4): ``GET /federation/status``.

A single read-only, best-effort endpoint that answers "what does this instance's
federation look like right now?" — the thing an operator hits first when a
cross-instance ("sovereign") conf join fails. It joins together state that
already exists but was scattered:

  * **identity**   — this instance's canonical FQID (``capauth.resolve_agent_
    identity().fqid``) + its public SFU ws url + public webui base.
  * **relays**     — the configured Nostr relays (``SKCHAT_NOSTR_RELAYS``).
  * **trust**      — the per-FQID allow policy from ``federation-trust.json``
    (full_access entries, default level, remote-role cap).
  * **peers**      — the pinned peer verification keys under
    ``federation-peers/*.asc`` (directory-key-pinning / TOFU).
  * **discovered** — focus hosts currently advertised on the relays (what a
    peer's ``discover_and_elect`` would see), via the live relay query.
  * **counts**     — live local conf / space counts + a few federation
    counters (cross-realm tokens minted / redeemed, process-lifetime).

EVERY field is wrapped so a missing capauth, an unreadable trust file, an
unreachable relay, or an absent registry degrades to a null/empty value with an
``errors`` note — the endpoint NEVER raises and NEVER 500s, so a monitor can
always poll it. Counters are in-process (single-replica) and reset on restart.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger("skchat.federation_status")

# ── in-process federation counters (single-replica; reset on restart) ─────────
# Bumped from the mint/redeem seams so the status surface can show throughput
# without a metrics backend. A multi-replica deployment would swap these for a
# shared store; for the sovereign single-box topology in-process is enough.
_COUNTERS: dict[str, int] = {
    "fed_tokens_minted": 0,
    "fed_tokens_redeemed": 0,
}


def incr(counter: str, n: int = 1) -> None:
    """Best-effort bump of a federation counter (unknown names are created)."""
    _COUNTERS[counter] = _COUNTERS.get(counter, 0) + n


def snapshot_counters() -> dict:
    return dict(_COUNTERS)


# ── identity / public endpoints ───────────────────────────────────────────────
def _self_fqid() -> str | None:
    try:
        from capauth import resolve_agent_identity

        return resolve_agent_identity().fqid
    except Exception as exc:  # noqa: BLE001 - capauth optional / unconfigured
        logger.debug("federation/status: fqid unresolved: %s", exc)
        return None


def _public_sfu_ws_url() -> str:
    pub = os.getenv("SKCHAT_LIVEKIT_PUBLIC_URL", "").strip()
    return pub or os.getenv("SKCHAT_LIVEKIT_URL", "").strip()


def _public_webui_base() -> str:
    explicit = os.getenv("SKCHAT_PUBLIC_WEBUI_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    # Derive from the public SFU host (same Funnel/tailnet host in Shape-B).
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(_public_sfu_ws_url()).hostname or "").strip()
        return f"https://{host}" if host else ""
    except Exception:  # noqa: BLE001
        return ""


def _relays() -> list[str]:
    return [r for r in os.getenv("SKCHAT_NOSTR_RELAYS", "").split(",") if r.strip()]


# ── trust policy ──────────────────────────────────────────────────────────────
def _trust_view(errors: list) -> dict:
    """Read-only view of the per-FQID trust policy. Never raises."""
    try:
        from skchat.spaces.federation.trust import TrustPolicy

        tp = TrustPolicy()
        return {
            "configured": tp.path.exists(),
            "path": str(tp.path),
            "full_access": sorted(tp._full),
            "default": tp._default.value,
            "remote_max_role": tp.remote_max_role,
        }
    except Exception as exc:  # noqa: BLE001 - missing/unreadable config → degrade
        errors.append(f"trust: {exc}")
        return {"configured": False, "full_access": [], "default": "deny"}


# ── pinned peer keys ──────────────────────────────────────────────────────────
def _pinned_peers(errors: list) -> list[str]:
    """List the FQIDs with a pinned verification key (``federation-peers/*.asc``).

    Returns only the FQIDs (the armored key bytes are intentionally NOT exposed
    on a status surface). Never raises.
    """
    try:
        base = Path.home() / ".skchat" / "federation-peers"
        if not base.is_dir():
            return []
        # The on-disk name is the fqid with unsafe chars neutralised; we surface
        # the stem so an operator can confirm WHICH peers are pinned.
        return sorted(p.stem for p in base.glob("*.asc") if p.is_file())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"peers: {exc}")
        return []


# ── discovered focus hosts (live relay query) ─────────────────────────────────
def _discovered_focus(relays: list[str], errors: list) -> list[dict]:
    """Focus hosts currently advertised on the relays (best-effort, never fatal).

    This is exactly what a peer's ``discover_and_elect`` / ``/sfu/candidates``
    would see — surfacing it here lets an operator confirm discovery WITHOUT a
    second tool. Empty list when no relays are configured or the query fails.
    """
    if not relays:
        return []
    hosts: list[dict] = []
    try:
        from skchat.spaces.federation.events import FOCUS_KIND, parse_focus_descriptor
        from skchat.spaces.federation.nostr_io import FederationNostr

        nostr = FederationNostr(relays=relays)
        seen: set[str] = set()
        for ev in nostr._query({"kinds": [FOCUS_KIND]}):
            try:
                d = parse_focus_descriptor(ev)
            except Exception:  # noqa: BLE001 - hostile/malformed relay event
                continue
            fqid = (d.get("host_fqid") or "").strip()
            auth_url = (d.get("auth_url") or "").strip()
            sfu_ws_url = (d.get("sfu_ws_url") or "").strip()
            if not (fqid and auth_url and sfu_ws_url) or fqid in seen:
                continue
            seen.add(fqid)
            hosts.append({"fqid": fqid, "auth_url": auth_url, "sfu_ws_url": sfu_ws_url})
    except Exception as exc:  # noqa: BLE001 - discovery best-effort, never 500
        errors.append(f"discovery: {exc}")
        return []
    return hosts


# ── live local counts ─────────────────────────────────────────────────────────
def _live_counts(errors: list) -> dict:
    """Live LOCAL conf + space counts. Best-effort; 0 on any read failure."""
    confs = 0
    spaces = 0
    try:
        from skchat.conf.room import ConfRegistry

        confs = len(ConfRegistry().list_live())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"confs: {exc}")
    try:
        from skchat.spaces.space import SpaceRegistry  # type: ignore

        spaces = len(SpaceRegistry().list_live())
    except Exception as exc:  # noqa: BLE001 - space registry layout varies; tolerate
        logger.debug("federation/status: space count unavailable: %s", exc)
    return {"live_confs": confs, "live_spaces": spaces}


def build_federation_status(
    *,
    fqid_fn=_self_fqid,
    relays_fn=_relays,
    trust_fn=_trust_view,
    peers_fn=_pinned_peers,
    discover_fn=_discovered_focus,
    counts_fn=_live_counts,
) -> dict:
    """Assemble the full federation status dict (seams injectable for tests).

    Never raises — each section is independently guarded and any failure is
    recorded under ``errors`` while the rest of the surface still renders.
    """
    errors: list = []
    relays = relays_fn()
    return {
        "service": "skchat-federation",
        "status": "ok",
        "identity": {
            "fqid": fqid_fn(),
            "public_sfu_ws_url": _public_sfu_ws_url(),
            "public_webui_base": _public_webui_base(),
        },
        "relays": relays,
        "trust": trust_fn(errors),
        "pinned_peers": peers_fn(errors),
        "discovered_focus": discover_fn(relays, errors),
        "counts": {
            **counts_fn(errors),
            **snapshot_counters(),
        },
        "errors": errors,
    }


def register_federation_status_routes(app: FastAPI) -> None:
    """Wire ``GET /federation/status`` onto ``app`` (read-only, never 500)."""

    @app.get("/federation/status")
    async def federation_status() -> JSONResponse:  # noqa: D401 - FastAPI handler
        """Read-only federation observability surface.

        Reports this instance's federation identity + public endpoints, the
        configured relays, the trust policy, pinned peer keys, the focus hosts
        currently discoverable on the relays, and live conf/space + token
        counters. Best-effort: any sub-failure degrades to empty/null with an
        ``errors`` note rather than a 500.
        """
        try:
            return JSONResponse(build_federation_status())
        except Exception as exc:  # noqa: BLE001 - belt & suspenders: never 500
            logger.warning("federation/status assembly failed: %s", exc)
            return JSONResponse(
                {
                    "service": "skchat-federation",
                    "status": "degraded",
                    "errors": [str(exc)],
                }
            )
