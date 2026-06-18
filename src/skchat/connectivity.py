"""Connectivity / ICE policy — sovereign-first ladder with a free-public fallback.

Tier 1: Tailscale (both peers on the tailnet) — no relay needed.
Tier 2: same-network / LAN — host candidates only (no servers emitted).
Tier 3: relay tier (cross-NAT). The relay tier itself has a precedence ladder so
        that public conf calls work WITHOUT self-hosting TURN:

            sovereign coturn  >  free public TURN  >  STUN-only  >  tailnet-direct

        - **Sovereign coturn** (PREFERRED): if ``SKCHAT_TURN_SECRET`` +
          ``SKCHAT_TURN_URLS`` are set, emit ephemeral REST credentials for the
          shared skstack coturn (use-auth-secret). Sovereign always overrides the
          free public relay below.
        - **Free public TURN** (fallback): when no sovereign coturn is configured,
          emit the Open Relay Project's free public TURN so symmetric-NAT joiners
          still get a relay. Env-overridable + toggleable (see env vars below).
        - **STUN**: always offered for cross-NAT (defaults to Google's free STUN
          when ``SKCHAT_STUN_URLS`` is unset). Covers the common cone-NAT case
          without any relay.
Tier 4: skmesh / netbird overlay — designed-for, not emitted here yet.

The static-auth-secret is read from SKCHAT_TURN_SECRET (sourced from the skstack
coturn config); only short-lived derived credentials ever leave this module.

Environment variables
----------------------
Sovereign coturn (preferred relay):
    SKCHAT_TURN_SECRET   coturn use-auth-secret; presence selects the sovereign
                         relay (and disables the free public TURN below).
    SKCHAT_TURN_URLS     comma-list of sovereign turn: URLs.
    SKCHAT_TURN_TTL      ephemeral credential TTL seconds (default 300).

STUN:
    SKCHAT_STUN_URLS     comma-list of stun: URLs. Defaults to Google's free STUN
                         (stun.l.google.com + stun1..4) when unset.

Free public TURN (fallback when no sovereign coturn):
    SKCHAT_PUBLIC_TURN_ENABLED  "true"/"false" toggle (default true).
    SKCHAT_PUBLIC_TURN_URLS     comma-list of free turn: URLs
                                (default: Open Relay :80, :443, :443?transport=tcp).
    SKCHAT_PUBLIC_TURN_USER     free TURN username (default "openrelayproject").
    SKCHAT_PUBLIC_TURN_CRED     free TURN credential (default "openrelayproject").
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

# Resolved once at import; restart the process to change it.
_TURN_TTL_SECONDS = int(os.getenv("SKCHAT_TURN_TTL", "300"))

# Free public defaults — used only when the operator has NOT configured a
# sovereign coturn / explicit STUN. These keep public conf calls working with
# zero self-hosting (Chef's tiered call: tailnet-direct → STUN → free TURN).
_DEFAULT_STUN_URLS = (
    "stun:stun.l.google.com:19302,"
    "stun:stun1.l.google.com:19302,"
    "stun:stun2.l.google.com:19302,"
    "stun:stun3.l.google.com:19302,"
    "stun:stun4.l.google.com:19302"
)
# Open Relay Project — free public TURN (no signup). Multiple ports/transports
# so restrictive networks can reach at least one (80, 443, 443/tcp).
_DEFAULT_PUBLIC_TURN_URLS = (
    "turn:openrelay.metered.ca:80,"
    "turn:openrelay.metered.ca:443,"
    "turn:openrelay.metered.ca:443?transport=tcp"
)
_DEFAULT_PUBLIC_TURN_USER = "openrelayproject"
_DEFAULT_PUBLIC_TURN_CRED = "openrelayproject"


def _split_urls(raw: str) -> list[str]:
    return [u.strip() for u in raw.split(",") if u.strip()]


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


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
        peer_hint: {"on_tailnet": bool, "same_subnet": bool} — reachability hints.

    Returns:
        {ice_servers, policy, preferred_tier, on_tailnet}.

    Relay-tier (Tier 3) precedence:
        sovereign coturn > free public TURN > STUN-only > tailnet-direct.
    """
    hint = peer_hint or {}
    on_tailnet = bool(hint.get("on_tailnet"))
    same_subnet = bool(hint.get("same_subnet"))

    # Tailnet / LAN tiers stay FIRST — on-tailnet / same-subnet joiners never hit
    # STUN/TURN at all (direct host candidates only).
    if on_tailnet:
        return {"ice_servers": [], "policy": "all", "preferred_tier": 1, "on_tailnet": True}
    if same_subnet:
        return {"ice_servers": [], "policy": "all", "preferred_tier": 2, "on_tailnet": False}

    # Tier 3 — cross-NAT relay tier.
    ice_servers: list[dict] = []

    # STUN: explicit override, else Google's free public STUN. Always offered so
    # cone-NAT peers connect directly without ever needing a relay.
    stun = os.getenv("SKCHAT_STUN_URLS")
    if stun is None:
        stun = _DEFAULT_STUN_URLS
    stun_urls = _split_urls(stun)
    if stun_urls:
        ice_servers.append({"urls": stun_urls})

    # TURN precedence: sovereign coturn is PREFERRED. If SKCHAT_TURN_SECRET +
    # SKCHAT_TURN_URLS are set, emit ephemeral REST creds and do NOT add the free
    # public relay (sovereign overrides free).
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
    elif _env_flag("SKCHAT_PUBLIC_TURN_ENABLED", True):
        # Free public TURN fallback (Open Relay Project) — lets symmetric-NAT
        # joiners relay without any self-hosted coturn. Env-overridable.
        public_urls = _split_urls(
            os.getenv("SKCHAT_PUBLIC_TURN_URLS", _DEFAULT_PUBLIC_TURN_URLS)
        )
        if public_urls:
            ice_servers.append(
                {
                    "urls": public_urls,
                    "username": os.getenv(
                        "SKCHAT_PUBLIC_TURN_USER", _DEFAULT_PUBLIC_TURN_USER
                    ),
                    "credential": os.getenv(
                        "SKCHAT_PUBLIC_TURN_CRED", _DEFAULT_PUBLIC_TURN_CRED
                    ),
                }
            )

    return {
        "ice_servers": ice_servers,
        "policy": "all",
        "preferred_tier": 3,
        "on_tailnet": False,
    }
