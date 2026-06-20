"""Runtime focus-advertise (C2): publish this instance's SFU focus + a room's
membership to the configured Nostr relay(s) so a federated peer can discover it.

The federation discovery layer (``nostr_io`` / ``discovery``) is code-complete but
was never *called* at runtime — nothing ever published a focus descriptor or a
membership, so a peer's ``discover_and_elect()`` always found nothing. This module
is the missing producer side: a single best-effort, never-fatal helper that the
conf-create (and space-create) paths invoke so a room created here becomes
discoverable elsewhere.

Design (mirrors the audio path's seams):
  * relays come from ``SKCHAT_NOSTR_RELAYS`` (comma-separated) — never hardcoded.
  * the focus descriptor's ``sfu_ws_url`` is this host's PUBLIC LiveKit ws URL
    (``SKCHAT_LIVEKIT_PUBLIC_URL`` → fallback ``SKCHAT_LIVEKIT_URL``).
  * the focus descriptor's ``auth_url`` is this host's PUBLIC conf-mint endpoint
    (``<public webui base>/conf/{room}/federated-token``); the webui base comes
    from ``SKCHAT_PUBLIC_WEBUI_URL`` (derived from the public LiveKit host if
    unset). For audio Spaces the mint path is ``/sfu/get``.
  * everything is wrapped so a relay/parse/import failure NEVER fails the create.

The ``nostr`` seam is injectable so the whole thing is unit-testable with a fake
publisher (no network, mirroring ``FederationNostr(publish=..., query=...)``).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger("skchat.spaces.federation.advertise")


def _relays() -> list[str]:
    return [r for r in os.getenv("SKCHAT_NOSTR_RELAYS", "").split(",") if r.strip()]


def _public_sfu_ws_url() -> str:
    """This host's PUBLIC LiveKit ws URL — what a remote peer should dial.

    ``SKCHAT_LIVEKIT_PUBLIC_URL`` is the tailnet/Funnel-public wss endpoint
    (e.g. ``wss://noroc2027.tail204f0c.ts.net/livekit-ws``); fall back to the
    tailnet ``SKCHAT_LIVEKIT_URL`` so a single-realm tailnet still federates.
    """
    pub = os.getenv("SKCHAT_LIVEKIT_PUBLIC_URL", "").strip()
    return pub or os.getenv("SKCHAT_LIVEKIT_URL", "").strip()


def _public_webui_base() -> str:
    """Public HTTPS base of this instance's webui (no trailing slash).

    Explicit ``SKCHAT_PUBLIC_WEBUI_URL`` wins. Otherwise derive ``https://<host>``
    from the public LiveKit ws URL's hostname (the SFU and webui share the same
    Funnel/tailnet host in the Shape-B topology), so a deployment that only set
    ``SKCHAT_LIVEKIT_PUBLIC_URL`` still advertises a reachable auth_url.
    """
    explicit = os.getenv("SKCHAT_PUBLIC_WEBUI_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    sfu = _public_sfu_ws_url()
    host = (urlsplit(sfu).hostname or "").strip()
    return f"https://{host}" if host else ""


def _auth_url(mint_path: str) -> str:
    """Full public auth_url for a federated-token mint path (e.g. the conf path)."""
    base = _public_webui_base()
    if not base:
        return ""
    return base + mint_path


def _build_nostr(nostr, relays: list[str]):
    """Return the injected ``nostr`` seam, or a real ``FederationNostr`` over
    ``relays``. Import is lazy so a host without skcomms still imports this module.
    """
    if nostr is not None:
        return nostr
    from skchat.spaces.federation.nostr_io import FederationNostr

    return FederationNostr(relays=relays)


def advertise_focus(
    *,
    host_fqid: str,
    room: str,
    title: str,
    mint_path: str,
    status: str = "live",
    nostr=None,
) -> bool:
    """Best-effort: advertise this instance as the SFU focus for ``room``.

    Publishes THREE federation events to the configured relays:
      1. a focus descriptor (``host_fqid`` → public {auth_url, sfu_ws_url}),
      2. a Space-state event for the room (so directory listings see it), and
      3. a membership tying ``room`` to ``host_fqid`` as its preferred focus —
         which is exactly what a peer's ``select_focus`` / ``discover_and_elect``
         reads to elect this host.

    ``mint_path`` is the room-relative federated-token endpoint on this host
    (``/conf/{room}/federated-token`` for confs, ``/sfu/get`` for audio Spaces).

    Returns ``True`` if at least the membership landed on a relay, ``False`` on
    any failure / no relays configured. NEVER raises — the caller's create path
    must not fail because a relay was unreachable.
    """
    try:
        relays = _relays()
        if not relays and nostr is None:
            logger.debug("advertise_focus: no SKCHAT_NOSTR_RELAYS configured; skipping")
            return False
        sfu_ws_url = _public_sfu_ws_url()
        auth_url = _auth_url(mint_path)
        if not (host_fqid and sfu_ws_url and auth_url):
            logger.warning(
                "advertise_focus: incomplete advertise context "
                "(host_fqid=%r sfu_ws_url=%r auth_url=%r); skipping",
                host_fqid,
                sfu_ws_url,
                auth_url,
            )
            return False

        fn = _build_nostr(nostr, relays)
        # 1. focus descriptor (host → endpoints) — what /sfu/candidates lists.
        fn.publish_focus(host_fqid=host_fqid, auth_url=auth_url, sfu_ws_url=sfu_ws_url)
        # 2. Space/room state (best-effort; gives directory views a title/status).
        fn.publish_space(space_id=room, title=title, host_fqid=host_fqid, status=status)
        # 3. membership: this room's preferred focus is THIS host — the record
        #    discover_and_elect()/select_focus() reads to elect us.
        ok = fn.publish_membership(
            fqid=host_fqid,
            space_id=room,
            foci_preferred=host_fqid,
            issued_at=int(time.time()),
        )
        logger.info(
            "advertised focus host=%s room=%s sfu=%s membership_ok=%s",
            host_fqid,
            room,
            sfu_ws_url,
            ok,
        )
        return bool(ok)
    except Exception as exc:  # noqa: BLE001 - advertise is best-effort, never fatal
        logger.warning("advertise_focus failed for room %s: %s", room, exc)
        return False


def advertise_conf(*, host_fqid: str, room: str, title: str, nostr=None) -> bool:
    """Advertise a conference room's focus (auth_url = its federated-token mint)."""
    return advertise_focus(
        host_fqid=host_fqid,
        room=room,
        title=title,
        mint_path=f"/conf/{room}/federated-token",
        nostr=nostr,
    )


def advertise_space(
    *, host_fqid: str, space_id: str, title: str, nostr=None
) -> bool:
    """Advertise an audio Space's focus (auth_url = this host's ``/sfu/get``)."""
    return advertise_focus(
        host_fqid=host_fqid,
        room=space_id,
        title=title,
        mint_path="/sfu/get",
        nostr=nostr,
    )
