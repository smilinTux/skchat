"""CR-3.4 fixture-replay core: the Phase-3 sufficiency gate helpers.

The server-side issuer shadow proves convergence on LIVE traffic, but traffic is
not guaranteed to hit every route class. The replay is the table-driven
complement: mint one HS256 session and one operator-audience token for the SAME
fingerprint, replay the ENTIRE capability route table with both credentials, and
assert per-route identical status codes. The full replay against a live enforce
app is an on-node operational run (scripts/issuer_replay.py); here we prove the
route enumeration and the per-route convergence predicate, which are what make a
divergence detectable at all.
"""

from __future__ import annotations

from types import SimpleNamespace

from skchat.issuer_replay import (
    build_probe_path,
    iter_capability_probes,
    replay_one,
)


def test_build_probe_path_substitutes_every_param():
    assert build_probe_path("/api/v1/thread/{thread_id}") == "/api/v1/thread/x"
    assert build_probe_path("/api/v1/groups/{group_id}/members") == "/api/v1/groups/x/members"
    assert build_probe_path("/health") == "/health"


def test_iter_capability_probes_covers_the_whole_table():
    from skchat.dataplane_auth import _ROUTE_CAPABILITY_RULES

    probes = list(iter_capability_probes())
    assert len(probes) == len(_ROUTE_CAPABILITY_RULES)
    assert all("{" not in path and "}" not in path for _m, path, _cap in probes)


class _FakeClient:
    """Returns a status keyed by the Authorization header value."""

    def __init__(self, by_token):
        self._by_token = by_token

    def request(self, method, path, headers=None, **_kw):
        token = (headers or {}).get("Authorization", "")
        return SimpleNamespace(status_code=self._by_token[token])


def test_replay_one_reports_convergence_when_statuses_match():
    client = _FakeClient({"hs": 422, "aud": 422})
    r = replay_one(
        client, "POST", "/api/v1/send", {"Authorization": "hs"}, {"Authorization": "aud"}
    )
    assert r.converged
    assert r.hs_status == 422 and r.aud_status == 422


def test_replay_one_flags_auth_divergence():
    # HS256 authorizes (200); the audience subject fails to resolve -> PDP 403.
    client = _FakeClient({"hs": 200, "aud": 403})
    r = replay_one(
        client, "GET", "/api/v1/inbox", {"Authorization": "hs"}, {"Authorization": "aud"}
    )
    assert not r.converged
    assert r.hs_status == 200 and r.aud_status == 403
