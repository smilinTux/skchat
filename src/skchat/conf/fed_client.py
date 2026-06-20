"""Conf federation CLIENT (B1-fedclient) — the missing client leg of Shape-A.

The conf federation SERVER route ``POST /conf/{room}/federated-token``
(:mod:`skchat.conf.routes`) mints a cross-instance conf token from a
capauth-signed FQID assertion. It is the conf parallel of the audio Space
``POST /sfu/get`` authd, and it accepts the SAME ``{claim, sig}`` assertion
format (see :mod:`skchat.spaces.federation.assertion`).

Until now that route had no client caller, so cross-instance ("sovereign") conf
join had never been exercised end-to-end. This module is that caller: an
instance/agent on box B mints a cross-realm conf token from box A by sending a
signed FQID assertion, then uses the returned ``{token, url}`` to join A's conf.

It deliberately mirrors :class:`skchat.spaces.federation.discovery`'s
``build_signed_assertion`` + ``get_token`` seam, so the audio and conf
federation clients share one signing/POST contract. Every external dependency
(HTTP POST, signing) is injectable for unit testing — no network, no keys.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Optional

from skchat.spaces.federation.assertion import Assertion, build_signed

logger = logging.getLogger(__name__)

# A post seam takes (url, json_body) and returns a tiny response shape exposing
# ``.status_code`` (int) and ``.json()`` (-> dict). Matches requests/httpx
# responses and trivial test fakes alike (same contract as discovery.PostFn).
PostFn = Callable[[str, dict], "object"]


class ConfAuthDenied(Exception):
    """The remote conf authd refused the assertion (HTTP 403)."""


class ConfFederationError(Exception):
    """The remote conf authd returned a non-403, non-2xx error."""


def _default_post() -> PostFn:
    # Lazy import — only exercised in production, never under test.
    def _post(url: str, body: dict) -> object:
        import requests

        return requests.post(url, json=body, timeout=10)

    return _post


def _self_fqid() -> str:
    """Canonical sovereign FQID for the running agent (``agent@operator.realm``).

    Same resolver the call routes and audio federation use
    (``capauth.resolve_agent_identity().fqid``) — e.g. ``lumina@chef.skworld``.
    """
    from capauth import resolve_agent_identity

    return resolve_agent_identity().fqid


def _conf_token_url(remote_auth_url: str, room: str) -> str:
    """Build the remote ``/conf/{room}/federated-token`` URL.

    ``remote_auth_url`` may be a bare host (``http://box-a:8765``) — in which
    case the conf path is appended — or the full federated-token URL already.
    """
    base = remote_auth_url.rstrip("/")
    if base.endswith("/federated-token"):
        return base
    if "/conf/" in base:
        # caller passed ``…/conf/<room>`` — append the action
        return f"{base}/federated-token"
    return f"{base}/conf/{room}/federated-token"


def build_signed_conf_assertion(*, fqid: str, room: str,
                                sign: Optional[Callable[[bytes], str]] = None) -> dict:
    """Build a fresh signed FQID assertion (``{claim, sig}``) for a conf room.

    A new nonce + current ``issued_at`` are minted per call so each redeem is
    replay-distinct (the server's nonce cache rejects reuse). The conf room id
    is carried in the assertion's ``space_id`` slot — the assertion schema is
    shared with audio Spaces, and the server binds the minted token to the
    requested ``{room}`` path, so this is the room the token is scoped to.
    """
    a = Assertion(
        fqid=fqid,
        space_id=room,
        issued_at=int(time.time()),
        nonce=uuid.uuid4().hex,
    )
    if sign is not None:
        return build_signed(a, sign=sign)
    return build_signed(a)


def mint_remote_conf_token(
    remote_auth_url: str,
    room: str,
    *,
    fqid: Optional[str] = None,
    post: Optional[PostFn] = None,
    sign: Optional[Callable[[bytes], str]] = None,
) -> dict:
    """Mint a cross-instance conf token from a remote host's conf authd.

    Builds a capauth-signed FQID assertion for ``room`` and POSTs it to the
    remote ``POST /conf/{room}/federated-token``. On success returns the server
    payload::

        {token, url, role, identity, conf_id, room}

    where ``token`` is a LiveKit JWT for the remote host's SFU and ``url`` is
    that SFU's websocket URL — together enough for the Flutter app / a browser
    to join the remote conf.

    Args:
        remote_auth_url: Remote host base URL (``http://box-a:8765``) or the
            full ``…/conf/{room}/federated-token`` URL.
        room: The remote conf room id to join.
        fqid: Override the signing identity (defaults to this agent's canonical
            ``capauth.resolve_agent_identity().fqid``).
        post: HTTP POST seam (injected in tests).
        sign: Signing seam (injected in tests; defaults to the capauth backend).

    Raises:
        ConfAuthDenied: server returned 403 (untrusted fqid, replay, bad sig).
        ConfFederationError: server returned any other non-2xx.
    """
    _post = post or _default_post()
    _fqid = fqid or _self_fqid()
    url = _conf_token_url(remote_auth_url, room)

    signed = build_signed_conf_assertion(fqid=_fqid, room=room, sign=sign)
    logger.debug("minting remote conf token: fqid=%s room=%s url=%s", _fqid, room, url)

    resp = _post(url, signed)
    status = getattr(resp, "status_code", 0)
    if status == 403:
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001 - body may be empty/non-JSON
            detail = {}
        raise ConfAuthDenied(f"conf authd denied at {url}: {detail}")
    if status == 404:
        raise ConfFederationError(f"conf room {room!r} not found at {url} (404)")
    if status < 200 or status >= 300:
        raise ConfFederationError(f"conf authd error {status} at {url}")
    # Federation observability: this instance just minted a cross-realm token
    # from a remote host (best-effort counter — never break the mint on it).
    try:
        from skchat.federation_status import incr

        incr("fed_tokens_minted")
    except Exception:  # noqa: BLE001
        pass
    return resp.json()
