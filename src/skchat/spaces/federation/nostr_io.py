"""Nostr relay I/O for federation discovery events (spec §7).

Wraps the skcomms nostr low-level (`_publish_to_relay`/`_query_relay`) so a host
can publish its focus descriptor + Space state, and a client can query the
memberships for a Space. Relay calls are behind injectable `publish`/`query`
seams, so the whole thing is testable with fakes (no network).

The build/parse codec lives in events.py (Task 4); this module only wires it to
relay transport.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from skchat.spaces.federation.events import (
    MEMBERSHIP_KIND,
    SPACE_KIND,
    build_focus_descriptor,
    build_membership,
    build_space_state,
    parse_membership,
)
from skchat.spaces.federation.focus import Membership

# A publish seam takes a built event dict and returns whether it landed.
PublishFn = Callable[[dict], bool]
# A query seam takes a Nostr filter dict and returns matching event dicts.
QueryFn = Callable[[dict], list]


def _default_publish(relays: Iterable[str]) -> PublishFn:
    # NOTE: verified import path 2026-06-13 —
    # skcomms.transports.nostr._publish_to_relay resolves.
    from skcomms.transports.nostr import _publish_to_relay

    def _pub(event: dict) -> bool:
        ok = False
        for relay in relays:
            ok = _publish_to_relay(relay, event) or ok
        return ok

    return _pub


def _default_query(relays: Iterable[str]) -> QueryFn:
    from skcomms.transports.nostr import _query_relay

    def _qry(filters: dict) -> list:
        out: list = []
        for relay in relays:
            out.extend(_query_relay(relay, filters))
        return out

    return _qry


class FederationNostr:
    """Publish/query federation discovery events to/from Nostr relays."""

    def __init__(
        self,
        relays: Optional[list[str]] = None,
        *,
        publish: Optional[PublishFn] = None,
        query: Optional[QueryFn] = None,
    ) -> None:
        self.relays = relays or []
        self._publish = publish or _default_publish(self.relays)
        self._query = query or _default_query(self.relays)

    def publish_focus(self, *, host_fqid: str, auth_url: str, sfu_ws_url: str) -> bool:
        ev = build_focus_descriptor(host_fqid=host_fqid, auth_url=auth_url,
                                    sfu_ws_url=sfu_ws_url)
        return self._publish(ev)

    def publish_space(self, *, space_id: str, title: str, host_fqid: str,
                      status: str) -> bool:
        ev = build_space_state(space_id=space_id, title=title,
                               host_fqid=host_fqid, status=status)
        return self._publish(ev)

    def publish_membership(self, *, fqid: str, space_id: str, foci_preferred: str,
                           issued_at: int) -> bool:
        ev = build_membership(fqid=fqid, space_id=space_id,
                              foci_preferred=foci_preferred, issued_at=issued_at)
        return self._publish(ev)

    def query_memberships(self, space_id: str) -> list[Membership]:
        filters = {"kinds": [MEMBERSHIP_KIND],
                   "#a": [f"{SPACE_KIND}:{space_id}"]}
        events = self._query(filters)
        return [parse_membership(ev) for ev in events]
