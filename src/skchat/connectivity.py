"""Connectivity / ICE policy - sovereign-only ladder, fails closed.

Tier 1: Tailscale (both peers on the tailnet) - no relay needed.
Tier 2: same-network / LAN - host candidates only (no servers emitted).
Tier 3: relay tier (cross-NAT). The relay tier uses ONLY the sovereign coturn -
        there is no third-party relay tier of any kind:

            sovereign coturn  >  STUN-only  >  (nothing - fail closed)

        - **Sovereign coturn** (the only relay): if ``SKCHAT_TURN_SECRET`` +
          ``SKCHAT_TURN_URLS`` are set, emit ephemeral REST credentials for the
          shared skstack coturn (use-auth-secret). This is the ONLY relay this
          module ever emits.
        - **STUN**: always offered for cross-NAT (defaults to Google's free STUN
          when ``SKCHAT_STUN_URLS`` is unset). Covers the common cone-NAT case
          without any relay.
        - **Fail closed**: when no sovereign coturn is configured, NO relay is
          emitted, off any provider. A cross-NAT call that needs a relay simply
          cannot connect rather than reaching a public, non-sovereign TURN host.
Tier 4: skmesh / netbird overlay - designed-for, not emitted here yet.

The static-auth-secret is read from SKCHAT_TURN_SECRET (sourced from the skstack
coturn config); only short-lived derived credentials ever leave this module.

Environment variables
----------------------
Sovereign coturn (the only relay):
    SKCHAT_TURN_SECRET   coturn use-auth-secret; presence selects the sovereign
                         relay. Absence means no relay is emitted at all.
    SKCHAT_TURN_URLS     comma-list of sovereign turn: URLs (TLS + udp forms).
    SKCHAT_TURN_TTL      ephemeral credential TTL seconds (default 300).

STUN:
    SKCHAT_STUN_URLS     comma-list of stun: URLs. Defaults to Google's free STUN
                         (stun.l.google.com + stun1..4) when unset.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time

_LOG = logging.getLogger(__name__)

# Resolved once at import; restart the process to change it.
_TURN_TTL_SECONDS = int(os.getenv("SKCHAT_TURN_TTL", "300"))

# Free public STUN default - used only when the operator has NOT configured an
# explicit SKCHAT_STUN_URLS. STUN never relays media, so this carries no
# sovereignty risk; it only helps cone-NAT peers connect directly.
_DEFAULT_STUN_URLS = (
    "stun:stun.l.google.com:19302,"
    "stun:stun1.l.google.com:19302,"
    "stun:stun2.l.google.com:19302,"
    "stun:stun3.l.google.com:19302,"
    "stun:stun4.l.google.com:19302"
)


def _split_urls(raw: str) -> list[str]:
    return [u.strip() for u in raw.split(",") if u.strip()]


def _turn_credentials(local_fqid: str, secret: str, ttl: int) -> tuple[str, str]:
    """coturn ``use-auth-secret`` REST credentials.

    username = "<unix-expiry>:<identity>"; credential = base64(HMAC-SHA1(secret, username)).
    """
    expiry = int(time.time()) + ttl
    username = f"{expiry}:{local_fqid}"
    credential = base64.b64encode(
        hmac.HMAC(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode("ascii")
    return username, credential


def ice_config(local_fqid: str, peer_fqid: str, peer_hint: dict | None = None) -> dict:
    """Return ICE config + preferred-tier policy for a call to ``peer_fqid``.

    Args:
        local_fqid: our capauth FQID (becomes the TURN credential identity).
        peer_fqid: the peer's FQID (informational; tier comes from peer_hint).
        peer_hint: {"on_tailnet": bool, "same_subnet": bool} - reachability hints.

    Returns:
        {ice_servers, policy, preferred_tier, on_tailnet}.

    Relay-tier (Tier 3) precedence:
        sovereign coturn > STUN-only > (nothing - fail closed, no third-party relay).
    """
    hint = peer_hint or {}
    on_tailnet = bool(hint.get("on_tailnet"))
    same_subnet = bool(hint.get("same_subnet"))

    # Tailnet / LAN tiers stay FIRST - on-tailnet / same-subnet joiners never hit
    # STUN/TURN at all (direct host candidates only).
    if on_tailnet:
        return {"ice_servers": [], "policy": "all", "preferred_tier": 1, "on_tailnet": True}
    if same_subnet:
        return {"ice_servers": [], "policy": "all", "preferred_tier": 2, "on_tailnet": False}

    # Tier 3 - cross-NAT relay tier.
    ice_servers: list[dict] = []

    # STUN: explicit override, else Google's free public STUN. Always offered so
    # cone-NAT peers connect directly without ever needing a relay.
    stun = os.getenv("SKCHAT_STUN_URLS")
    if stun is None:
        stun = _DEFAULT_STUN_URLS
    stun_urls = _split_urls(stun)
    if stun_urls:
        ice_servers.append({"urls": stun_urls})

    # TURN: the sovereign coturn is the ONLY relay this module ever emits. If
    # SKCHAT_TURN_SECRET + SKCHAT_TURN_URLS are set, emit ephemeral REST creds.
    # If they are not set, no relay is emitted at all - fail closed. There is no
    # third-party fallback tier and no environment variable that can summon one.
    secret = os.getenv("SKCHAT_TURN_SECRET", "")
    urls_raw = os.getenv("SKCHAT_TURN_URLS", "")
    if secret and urls_raw:
        username, credential = _turn_credentials(local_fqid, secret, _TURN_TTL_SECONDS)
        ice_servers.append(
            {
                "urls": _split_urls(urls_raw),
                "username": username,
                "credential": credential,
            }
        )

    return {
        "ice_servers": ice_servers,
        "policy": "all",
        "preferred_tier": 3,
        "on_tailnet": False,
    }
