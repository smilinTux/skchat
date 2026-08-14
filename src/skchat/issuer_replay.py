"""CR-3.4 fixture replay: the Phase-3 sufficiency gate for issuer convergence.

The server-side issuer shadow (``dataplane_auth._issuer_shadow_compare``) proves
the audience path converges on LIVE traffic, but live traffic is not guaranteed to
exercise every route class. This module is the table-driven complement: it mints
one HS256 session and one operator-audience token for the SAME enrolled
fingerprint, replays the ENTIRE capability route table against a local enforce
instance with BOTH credentials, and asserts per-route identical status codes. The
table, not traffic, proves completeness (the coverage-gate philosophy applied to
issuers).

The value being probed is per-route AGREEMENT, not success: the two credentials
are sent the SAME request, so a 404/422 on a nonexistent id is fine as long as
BOTH produce it. A one-sided 401/403 is exactly the issuer divergence the flip
must not carry (R1/R10 in the spec).

The pure helpers (route enumeration + the convergence predicate) live here so they
are unit-tested; ``scripts/issuer_replay.py`` is the thin operational CLI that
stands up the app, enrolls a device, mints both credentials, and drives them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Mapping

from .dataplane_auth import _ROUTE_CAPABILITY_RULES

_PARAM = re.compile(r"\{[^/}]+\}")


def build_probe_path(pattern: str, placeholder: str = "x") -> str:
    """Concretize a route pattern by replacing each ``{param}`` with one segment.

    The placeholder value is irrelevant: the replay compares the HS256 and
    audience responses to the SAME request, so a 404/422 on a nonexistent id is a
    convergence as long as both credentials produce it.
    """
    return _PARAM.sub(placeholder, pattern)


def iter_capability_probes(placeholder: str = "x") -> Iterator[tuple[str, str, str]]:
    """Yield ``(method, concrete_path, capability)`` for every capability rule."""
    for method, pattern, cap in _ROUTE_CAPABILITY_RULES:
        yield method.upper(), build_probe_path(pattern, placeholder), cap


@dataclass(frozen=True)
class RouteResult:
    method: str
    path: str
    hs_status: int
    aud_status: int

    @property
    def converged(self) -> bool:
        return self.hs_status == self.aud_status


def replay_one(
    client,
    method: str,
    path: str,
    hs_headers: Mapping[str, str],
    aud_headers: Mapping[str, str],
) -> RouteResult:
    """Send the SAME request twice (HS256 then audience) and compare statuses.

    ``client`` is anything with ``request(method, path, headers=...)`` returning an
    object with ``status_code`` (a FastAPI ``TestClient`` or an httpx client).
    """
    hs = client.request(method, path, headers=dict(hs_headers))
    aud = client.request(method, path, headers=dict(aud_headers))
    return RouteResult(method, path, hs.status_code, aud.status_code)
