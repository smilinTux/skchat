"""Federation discovery client (spec §7, U8).

A client that wants to join a federated Space needs to (1) discover which hosts
advertise the Space and which SFU "focus" has been elected, (2) build a signed
FQID assertion, and (3) redeem it at the elected host's live ``/sfu/get`` authd
route for a LiveKit token.

This module wires the existing, individually-tested seams together:

* ``nostr_io.FederationNostr.query_memberships`` → presence events → focus election
* ``events.FOCUS_KIND`` / ``parse_focus_descriptor`` → host→endpoint mapping
* ``focus.select_focus`` → deterministic oldest-host-wins election
* ``assertion.build_signed`` → the signed {claim, sig} body for ``/sfu/get``

Every external dependency (Nostr relay query, HTTP POST, signing) is injectable so
the whole flow is unit-testable with fakes — no network, no relays, no keys.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from skchat.spaces.federation.assertion import Assertion, build_signed
from skchat.spaces.federation.events import (
    FOCUS_KIND,
    parse_focus_descriptor,
)
from skchat.spaces.federation.focus import select_focus
from skchat.spaces.federation.nostr_io import FederationNostr

logger = logging.getLogger(__name__)

# A post seam takes (url, json_body) and returns a tiny response shape:
# an object exposing ``.status_code`` (int) and ``.json()`` (-> dict). This
# matches both ``requests``/``httpx`` responses and trivial test fakes.
PostFn = Callable[[str, dict], "object"]


class AuthDenied(Exception):
    """The elected host's authd refused the assertion (HTTP 403)."""


class DiscoveryError(Exception):
    """Discovery could not resolve a usable host/focus for the Space."""


@dataclass
class ElectedHost:
    """The result of an election: the winning SFU focus + how to reach it."""

    fqid: str
    auth_url: str
    sfu_ws_url: str


def _default_post() -> PostFn:
    # Lazy import — only exercised in production, never under test.
    def _post(url: str, body: dict) -> object:
        import requests

        return requests.post(url, json=body, timeout=10)

    return _post


class FederationDiscoveryClient:
    """Discover a federated Space's elected SFU and redeem a token for it."""

    def __init__(
        self,
        *,
        nostr: Optional[FederationNostr] = None,
        relays: Optional[list[str]] = None,
        post: Optional[PostFn] = None,
        sign: Optional[Callable[[bytes], str]] = None,
    ) -> None:
        # ``nostr`` may be injected directly (tests pass a fake-relay-backed one);
        # otherwise build a real FederationNostr over the given relays.
        self._nostr = nostr or FederationNostr(relays=relays or [])
        self._post = post or _default_post()
        # ``sign`` defaults to assertion.build_signed's own capauth default when
        # None (we only override it when explicitly injected).
        self._sign = sign

    # ── focus descriptors ────────────────────────────────────────────────────
    def _focus_descriptors(self) -> dict[str, ElectedHost]:
        """Query the relay for focus descriptors → {host_fqid: ElectedHost}.

        Malformed / non-JSON descriptor events are skipped (M2) so one hostile
        relay record can't break discovery.
        """
        filters = {"kinds": [FOCUS_KIND]}
        out: dict[str, ElectedHost] = {}
        for ev in self._nostr._query(filters):
            try:
                d = parse_focus_descriptor(ev)
            except Exception as exc:  # noqa: BLE001 - defensive, hostile relay
                logger.warning("skipping malformed focus descriptor: %s", exc)
                continue
            host = (d.get("host_fqid") or "").strip()
            auth_url = (d.get("auth_url") or "").strip()
            sfu_ws_url = (d.get("sfu_ws_url") or "").strip()
            if not (host and auth_url and sfu_ws_url):
                continue
            out[host] = ElectedHost(fqid=host, auth_url=auth_url, sfu_ws_url=sfu_ws_url)
        return out

    def discover_and_elect(self, space_id: str) -> ElectedHost:
        """Discover memberships for ``space_id`` and elect the SFU focus.

        Election rule mirrors ``focus.select_focus`` exactly: the oldest valid
        membership's ``foci_preferred`` wins (ties broken by lowest fqid). The
        elected focus fqid is then resolved to its advertised endpoints via the
        focus descriptors. Raises ``DiscoveryError`` if no focus can be elected
        or the elected focus has no reachable descriptor.
        """
        memberships = self._nostr.query_memberships(space_id)
        elected_fqid = select_focus(memberships)
        if not elected_fqid:
            raise DiscoveryError(f"no focus elected for space {space_id!r}")
        descriptors = self._focus_descriptors()
        host = descriptors.get(elected_fqid)
        if host is None:
            raise DiscoveryError(f"no focus descriptor for elected host {elected_fqid!r}")
        return host

    # ── signed assertion ─────────────────────────────────────────────────────
    def build_signed_assertion(self, *, fqid: str, space_id: str) -> dict:
        """Build a fresh signed FQID assertion ({claim, sig}) for ``/sfu/get``.

        A new nonce + current ``issued_at`` are minted per call so each redeem is
        replay-distinct (authd's nonce cache rejects reuse).
        """
        a = Assertion(
            fqid=fqid,
            space_id=space_id,
            issued_at=int(time.time()),
            nonce=uuid.uuid4().hex,
        )
        if self._sign is not None:
            return build_signed(a, sign=self._sign)
        return build_signed(a)

    # ── token redemption ─────────────────────────────────────────────────────
    def get_token(self, host: ElectedHost, *, fqid: str, space_id: str) -> dict:
        """Redeem a signed assertion at the elected host's live ``/sfu/get``.

        Posts the {claim, sig} body to ``host.auth_url``. A 403 maps to
        ``AuthDenied``; any other non-2xx maps to ``DiscoveryError``. On success
        returns the authd payload (token, role, sfu_ws_url, identity, space_id).
        """
        signed = self.build_signed_assertion(fqid=fqid, space_id=space_id)
        resp = self._post(host.auth_url, signed)
        status = getattr(resp, "status_code", 0)
        if status == 403:
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001 - body may be empty/non-JSON
                detail = {}
            raise AuthDenied(f"authd denied at {host.auth_url}: {detail}")
        if status < 200 or status >= 300:
            raise DiscoveryError(f"authd error {status} at {host.auth_url}")
        return resp.json()
