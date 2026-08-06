"""Route-coverage completeness gate (SKWorld Authorization Model L1.3 / L2.5).

This is the test that would have caught incident (b): the CR-3 enforce flip 403ed
``GET /api/v1/conversations`` and ``GET /api/v1/status`` because the old suffix map
covered only 3 of 30+ gated routes, and shadow mode is STRUCTURALLY BLIND to
unmapped routes ("shadow divergence == 0" was never sufficient).

It enumerates the LIVE FastAPI route table and asserts every route is in exactly
one class:

  * gated + capability-mapped  (``route_capability`` returns a capability), or
  * gated + self-auth          (in ``SELF_AUTH_ROUTES``, own verifier), or
  * public                     (``is_gated`` returns False).

A new gated route that skips classification breaks CI the same day, not at the
enforce flip months later. This gate -- not shadow soak -- is the enforce-safety
criterion (shadow validates the mapped DECISIONS; the gate validates the MAP).
"""

from __future__ import annotations

import pytest

from skchat import dataplane_auth
from skchat.dataplane_auth import (
    _COMPILED_CAPABILITY,
    _COMPILED_SELF_AUTH,
    is_self_auth_route,
    route_capability,
)
from skchat.dataplane_paths import is_gated


def _served_routes() -> list[tuple[str, str]]:
    """Every served ``(METHOD, path_format)`` on the real app (HTTP only).

    Websocket routes (no ``.methods``) are the documented ``/ws/*`` follow-up and
    are excluded; HEAD/OPTIONS are auto-added by Starlette and carry no auth
    semantics of their own.
    """
    from skchat.webui import app

    out: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue  # websocket / mount without HTTP methods
        path = getattr(route, "path_format", None) or getattr(route, "path", None)
        if not path:
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            out.append((method.upper(), path))
    return out


def test_every_gated_route_is_classified():
    """COMPLETENESS: every gated route resolves to a capability OR is self-auth.

    Run against the pre-fix suffix map (only /api/send, /api/v1/prekey,
    /api/v1/inbox), this fails immediately on GET /api/v1/conversations,
    GET /api/v1/conversations/{peer_id}, GET /api/v1/status and ~40 siblings.
    """
    unclassified: list[str] = []
    for method, path in _served_routes():
        if not is_gated(method, path):
            continue
        if route_capability(method, path) is not None:
            continue
        if is_self_auth_route(method, path):
            continue
        unclassified.append(f"{method} {path}")

    assert not unclassified, (
        "gated routes with no capability mapping and not in the self-auth "
        "registry (unsafe to flip enforce -- incident b class):\n  "
        + "\n  ".join(sorted(unclassified))
    )


def test_no_dead_capability_mappings():
    """NO DEAD MAPPINGS: every capability rule matches >=1 served gated route.

    Drift guard (typo / removed route). A rule that matches nothing is either a
    typo or references a route that no longer exists.
    """
    served = _served_routes()
    dead: list[str] = []
    for method, rx, cap in _COMPILED_CAPABILITY:
        hit = any(
            m == method and rx.match(path) and is_gated(m, path) for m, path in served
        )
        if not hit:
            dead.append(f"{method} {rx.pattern} -> {cap}")
    assert not dead, "capability rules matching no served gated route:\n  " + "\n  ".join(dead)


def test_no_dead_self_auth_mappings():
    """Every self-auth registry entry matches >=1 served gated route."""
    served = _served_routes()
    dead: list[str] = []
    for method, rx, verifier, _rationale in _COMPILED_SELF_AUTH:
        hit = any(
            m == method and rx.match(path) and is_gated(m, path) for m, path in served
        )
        if not hit:
            dead.append(f"{method} {rx.pattern} ({verifier})")
    assert not dead, "self-auth rules matching no served gated route:\n  " + "\n  ".join(dead)


def test_every_mapped_capability_has_a_pdp_rule():
    """KNOWN CAPABILITIES ONLY: every mapped capability has a rule row in the PDP.

    Without a rule, ``decide`` fails closed on "unknown capability" and enforce
    would 403 the route despite a mapping.
    """
    from capauth.authz import DEFAULT_RULES

    mapped = {cap for _m, _rx, cap in _COMPILED_CAPABILITY}
    missing = sorted(mapped - set(DEFAULT_RULES))
    assert not missing, f"mapped capabilities with no capauth PDP rule row: {missing}"


def test_public_allowlist_is_consistent_with_is_gated():
    """No route is BOTH public-listed and gated; every public entry is is_gated False.

    Uses the explicit ``PUBLIC_ROUTES`` structure (L1.4) as a reviewed declaration:
    a route the allowlist calls public must actually be exempt in the gated
    classifier, or the two disagree and one is wrong.
    """
    contradictions: list[str] = []
    for method, path, _rationale in dataplane_auth.PUBLIC_ROUTES:
        if is_gated(method, path):
            contradictions.append(f"{method} {path}")
    assert not contradictions, (
        "routes on the public allowlist that is_gated says are GATED "
        "(classifier disagreement):\n  " + "\n  ".join(contradictions)
    )


def test_self_auth_and_capability_maps_are_disjoint():
    """A route is never both capability-mapped and self-auth (single class, L1.3)."""
    both: list[str] = []
    for method, path in _served_routes():
        if not is_gated(method, path):
            continue
        if route_capability(method, path) is not None and is_self_auth_route(method, path):
            both.append(f"{method} {path}")
    assert not both, "routes classified as BOTH capability and self-auth:\n  " + "\n  ".join(both)


def test_incident_b_routes_now_resolve():
    """Regression pins for the two incident-(b) routes that 403ed the enforce flip."""
    assert route_capability("GET", "/api/v1/conversations") == "skchat.inbox"
    assert route_capability("GET", "/api/v1/conversations/deadbeef") == "skchat.inbox"
    assert route_capability("GET", "/api/v1/status") == "skchat.status"


@pytest.mark.parametrize(
    "method,path,expected",
    [
        # Method awareness: GET reads own inbox; POST is federation self-auth.
        ("GET", "/api/v1/inbox", "skchat.inbox"),
        ("POST", "/api/v1/inbox", None),  # federation exempt (is_gated False)
        # Group READ vs group MUTATE split on the same prefix.
        ("GET", "/api/v1/groups", "skchat.inbox"),
        ("POST", "/api/v1/groups", "skchat.groups"),
        ("GET", "/api/v1/groups/g1/members", "skchat.inbox"),
        ("POST", "/api/v1/groups/g1/members", "skchat.groups"),
        ("GET", "/api/v1/groups/g1/call/participants", "skchat.status"),
        ("POST", "/api/v1/groups/g1/call/start", "skchat.calls"),
        # Public prekey directory (GET) vs gated publish (POST).
        ("POST", "/api/v1/prekey", "skchat.prekey"),
        ("GET", "/api/v1/prekey/alice", None),
        # Parameterized reads resolve on concrete paths.
        ("GET", "/file/xfer123", "skchat.inbox"),
        ("GET", "/file/xfer123/thumb", "skchat.inbox"),
        ("POST", "/upload", "skchat.media.write"),
        ("POST", "/api/v1/transcribe", "skchat.voice"),
        ("POST", "/api/v1/access/token", "skchat.calls"),
    ],
)
def test_method_aware_classifications(method, path, expected):
    assert route_capability(method, path) == expected


def test_token_mints_are_self_auth_not_capability():
    """The two gated token mints authenticate on their own terms (never the PDP)."""
    for method, path in (("POST", "/api/v1/audience-token"), ("POST", "/api/v1/embed-token")):
        assert is_gated(method, path) is True
        assert route_capability(method, path) is None
        assert is_self_auth_route(method, path) is True
