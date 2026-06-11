"""Connectivity / ICE policy — sovereign-first ladder.

Tier 1: Tailscale (both peers on the tailnet) — no relay needed.
Tier 2: same-network / LAN — host candidates only (no servers emitted).
Tier 3: coturn TURN via the shared skstack coturn (skhub.<cluster>.<domain>:3478),
        ephemeral REST credentials (use-auth-secret). Same coturn as Nextcloud/netbird.
Tier 4: skmesh / netbird overlay — designed-for, not emitted here yet.

The static-auth-secret is read from SKCHAT_TURN_SECRET (sourced from the skstack
coturn config); only short-lived derived credentials ever leave this module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

_TURN_TTL_SECONDS = int(os.getenv("SKCHAT_TURN_TTL", "300"))


def _turn_credentials(local_fqid: str, secret: str, ttl: int) -> tuple[str, str]:
    """coturn ``use-auth-secret`` REST credentials.

    username = "<unix-expiry>:<identity>"; credential = base64(HMAC-SHA1(secret, username)).
    """
    expiry = int(time.time()) + ttl
    username = f"{expiry}:{local_fqid}"
    credential = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode("ascii")
    return username, credential


def ice_config(local_fqid: str, peer_fqid: str, peer_hint: dict | None = None) -> dict:
    """Return ICE config + preferred-tier policy for a call to ``peer_fqid``.

    Args:
        local_fqid: our capauth FQID (becomes the TURN credential identity).
        peer_fqid: the peer's FQID (informational; tier comes from peer_hint).
        peer_hint: {"on_tailnet": bool, "same_subnet": bool} — reachability hints.

    Returns:
        {ice_servers, policy, preferred_tier, on_tailnet}.
    """
    hint = peer_hint or {}
    on_tailnet = bool(hint.get("on_tailnet"))
    same_subnet = bool(hint.get("same_subnet"))

    if on_tailnet:
        return {"ice_servers": [], "policy": "all", "preferred_tier": 1, "on_tailnet": True}
    if same_subnet:
        return {"ice_servers": [], "policy": "all", "preferred_tier": 2, "on_tailnet": False}

    # Tier 3 — coturn relay.
    secret = os.getenv("SKCHAT_TURN_SECRET", "")
    urls_raw = os.getenv("SKCHAT_TURN_URLS", "")
    ice_servers: list[dict] = []
    stun = os.getenv("SKCHAT_STUN_URLS", "")
    if stun:
        ice_servers.append({"urls": [u.strip() for u in stun.split(",") if u.strip()]})
    if secret and urls_raw:
        username, credential = _turn_credentials(local_fqid, secret, _TURN_TTL_SECONDS)
        ice_servers.append(
            {
                "urls": [u.strip() for u in urls_raw.split(",") if u.strip()],
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
