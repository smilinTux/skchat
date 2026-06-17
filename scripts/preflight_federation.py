#!/usr/bin/env python3
"""Preflight the U8 federated-discovery client logic — locally, no live infra.

Composes the REAL shipped components end-to-end:

* ``FederationDiscoveryClient`` (src/skchat/spaces/federation/discovery.py)
* ``FederationNostr`` query seam (nostr_io.py)
* ``select_focus`` deterministic election (focus.py)
* ``build_signed`` / ``Assertion`` codec (assertion.py)
* ``GET /sfu/candidates`` FastAPI route (spaces/routes.py)

Fakes live ONLY at the two true external boundaries:

1. the Nostr relay — an in-proc fake relay advertising fake host
   membership + focus-descriptor events (injected via the ``query`` seam for the
   discovery client, and via ``skcomms.transports.nostr._query_relay`` monkeypatch
   for the TestClient path);
2. the SFU ``/sfu/get`` HTTP POST — a fake ``post`` returning a 200 token, and a
   403 to exercise the ``AuthDenied`` path.

No network, no relays, no keys, no GPU. Exits 0 on PASS, non-zero on FAIL.
"""

from __future__ import annotations

import json
import sys
import traceback

# ── REAL components under test ───────────────────────────────────────────────
from skchat.spaces.federation.discovery import (
    AuthDenied,
    DiscoveryError,
    ElectedHost,
    FederationDiscoveryClient,
)
from skchat.spaces.federation.events import (
    FOCUS_KIND,
    MEMBERSHIP_KIND,
    build_focus_descriptor,
    build_membership,
)
from skchat.spaces.federation.nostr_io import FederationNostr


# ── Fake #1: in-proc Nostr relay (the ONLY relay-boundary fake) ──────────────
class _FakeRelay:
    """Returns canned membership / focus events keyed by the query's ``kinds``."""

    def __init__(self, membership_events=None, focus_events=None):
        self._memberships = membership_events or []
        self._focus = focus_events or []
        self.queries: list[dict] = []

    def query(self, filters: dict) -> list:
        self.queries.append(filters)
        kinds = filters.get("kinds") or []
        if FOCUS_KIND in kinds:
            return list(self._focus)
        if MEMBERSHIP_KIND in kinds:
            return list(self._memberships)
        return []


# ── Fake #2: SFU /sfu/get HTTP boundary ──────────────────────────────────────
class _FakeResp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _nostr(relay: _FakeRelay) -> FederationNostr:
    # Inject the fake query seam; publish is never exercised in this preflight.
    return FederationNostr(publish=lambda ev: True, query=relay.query)


# ── Fixtures: 3 fake hosts advertising membership + focus descriptors ────────
SPACE_ID = "space-preflight"

# Three members. Oldest issued_at -> its preferred focus wins (focus.py rule).
MEMBERSHIPS = [
    build_membership(
        fqid="c@chef.skworld",
        space_id=SPACE_ID,
        foci_preferred="lumina@chef.skworld",
        issued_at=300,
    ),
    build_membership(
        fqid="b@chef.skworld",
        space_id=SPACE_ID,
        foci_preferred="jarvis@chef.skworld",
        issued_at=200,
    ),
    build_membership(
        fqid="a@chef.skworld",
        space_id=SPACE_ID,
        foci_preferred="opus@chef.skworld",
        issued_at=100,  # OLDEST -> opus is the deterministic elected focus
    ),
]
EXPECTED_FOCUS = "opus@chef.skworld"

FOCUS_DESCRIPTORS = [
    build_focus_descriptor(
        host_fqid="opus@chef.skworld",
        auth_url="https://opus.skworld/sfu/get",
        sfu_ws_url="wss://opus.skworld:8443",
    ),
    build_focus_descriptor(
        host_fqid="lumina@chef.skworld",
        auth_url="https://lumina.skworld/sfu/get",
        sfu_ws_url="wss://lumina.skworld:8443",
    ),
    build_focus_descriptor(
        host_fqid="jarvis@chef.skworld",
        auth_url="https://jarvis.skworld/sfu/get",
        sfu_ws_url="wss://jarvis.skworld:8443",
    ),
]


# ── Assertion helpers ────────────────────────────────────────────────────────
class CheckFail(AssertionError):
    pass


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckFail(msg)


def check_discover_and_elect_oldest_host_wins() -> str:
    relay = _FakeRelay(membership_events=MEMBERSHIPS, focus_events=FOCUS_DESCRIPTORS)
    client = FederationDiscoveryClient(nostr=_nostr(relay))
    host = client.discover_and_elect(SPACE_ID)
    _check(isinstance(host, ElectedHost), "discover_and_elect must return an ElectedHost")
    _check(
        host.fqid == EXPECTED_FOCUS,
        f"elected focus must be oldest-host {EXPECTED_FOCUS!r}, got {host.fqid!r}",
    )
    _check(
        host.auth_url == "https://opus.skworld/sfu/get",
        f"auth_url resolved from descriptor mismatch: {host.auth_url!r}",
    )
    _check(
        host.sfu_ws_url == "wss://opus.skworld:8443",
        f"sfu_ws_url resolved from descriptor mismatch: {host.sfu_ws_url!r}",
    )
    # determinism: a second run over the same relay yields the identical winner.
    host2 = FederationDiscoveryClient(nostr=_nostr(relay)).discover_and_elect(SPACE_ID)
    _check(host2.fqid == host.fqid, "election is non-deterministic across runs")
    return f"elected focus = {host.fqid} (oldest host, deterministic)"


def check_build_signed_assertion_shape() -> str:
    relay = _FakeRelay()
    client = FederationDiscoveryClient(nostr=_nostr(relay), sign=lambda payload: "FAKE-SIG")
    signed = client.build_signed_assertion(fqid="a@chef.skworld", space_id=SPACE_ID)
    _check(set(signed.keys()) == {"claim", "sig"}, f"signed body must be {{claim,sig}}: {signed}")
    _check(signed["sig"] == "FAKE-SIG", "injected sign() was not used")
    claim = json.loads(signed["claim"])
    _check(claim["fqid"] == "a@chef.skworld", "claim.fqid mismatch")
    _check(claim["space_id"] == SPACE_ID, "claim.space_id mismatch")
    _check(bool(claim["nonce"]), "claim.nonce must be a fresh non-empty nonce")
    _check("issued_at" in claim, "claim.issued_at missing")
    # fresh nonce per call (replay-distinct redeems)
    n2 = json.loads(
        client.build_signed_assertion(fqid="a@chef.skworld", space_id=SPACE_ID)["claim"]
    )["nonce"]
    _check(n2 != claim["nonce"], "nonce must be fresh on each build")
    return "build_signed_assertion -> {claim, sig}, fresh nonce per call"


def check_get_token_request_shape_200() -> str:
    host = ElectedHost(
        fqid=EXPECTED_FOCUS,
        auth_url="https://opus.skworld/sfu/get",
        sfu_ws_url="wss://opus.skworld:8443",
    )
    captured: dict = {}

    def fake_post(url, body):
        captured["url"] = url
        captured["body"] = body
        return _FakeResp(
            200,
            {
                "token": "JWT-CROSS-HOST",
                "role": "speaker",
                "sfu_ws_url": host.sfu_ws_url,
                "identity": "a@chef.skworld",
                "space_id": SPACE_ID,
            },
        )

    client = FederationDiscoveryClient(
        nostr=_nostr(_FakeRelay()), post=fake_post, sign=lambda p: "FAKE-SIG"
    )
    out = client.get_token(host, fqid="a@chef.skworld", space_id=SPACE_ID)
    # request shape: POSTed to the elected host's auth_url with a {claim, sig} body
    _check(captured["url"] == host.auth_url, f"POST url must be {host.auth_url!r}")
    _check(set(captured["body"].keys()) == {"claim", "sig"}, "POST body must be {claim, sig}")
    _check(captured["body"]["sig"] == "FAKE-SIG", "POST body.sig mismatch")
    claim = json.loads(captured["body"]["claim"])
    _check(claim["fqid"] == "a@chef.skworld" and claim["space_id"] == SPACE_ID, "POST claim shape")
    # response passthrough
    _check(out["token"] == "JWT-CROSS-HOST", "token not returned from authd payload")
    _check(out["role"] == "speaker", "role not returned from authd payload")
    return "get_token 200 -> POST {claim,sig} to auth_url, token returned"


def check_get_token_403_auth_denied() -> str:
    host = ElectedHost(
        fqid=EXPECTED_FOCUS,
        auth_url="https://opus.skworld/sfu/get",
        sfu_ws_url="wss://opus.skworld:8443",
    )
    client = FederationDiscoveryClient(
        nostr=_nostr(_FakeRelay()),
        post=lambda url, body: _FakeResp(403, {"detail": "fqid not permitted"}),
        sign=lambda p: "FAKE-SIG",
    )
    try:
        client.get_token(host, fqid="evil@attacker", space_id=SPACE_ID)
    except AuthDenied:
        return "get_token 403 -> AuthDenied (as required)"
    raise CheckFail("a 403 from /sfu/get must raise AuthDenied")


def check_get_token_500_discovery_error() -> str:
    host = ElectedHost(
        fqid=EXPECTED_FOCUS,
        auth_url="https://opus.skworld/sfu/get",
        sfu_ws_url="wss://opus.skworld:8443",
    )
    client = FederationDiscoveryClient(
        nostr=_nostr(_FakeRelay()),
        post=lambda url, body: _FakeResp(500),
        sign=lambda p: "FAKE-SIG",
    )
    try:
        client.get_token(host, fqid="a@chef.skworld", space_id=SPACE_ID)
    except DiscoveryError:
        return "get_token non-2xx (500) -> DiscoveryError"
    raise CheckFail("a non-2xx (non-403) from /sfu/get must raise DiscoveryError")


def check_sfu_candidates_route_shape() -> str:
    """Hit the REAL GET /sfu/candidates via FastAPI TestClient.

    The relay boundary is faked by monkeypatching skcomms.transports.nostr._query_relay
    (the same seam the production route reaches through FederationNostr).
    """
    import os
    from unittest import mock

    import skcomms.transports.nostr as nostr_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from skchat.spaces.registry import SpaceRegistry
    from skchat.spaces.routes import register_spaces_routes

    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry())
    client = TestClient(app)

    # advertise our 3 fake focus descriptors (plus a dup + a malformed event to
    # exercise dedup/skip), through the real route's relay query path.
    dup = build_focus_descriptor(
        host_fqid="opus@chef.skworld",
        auth_url="https://opus.skworld/sfu/get",
        sfu_ws_url="wss://opus.skworld:8443",
    )
    malformed = {"kind": FOCUS_KIND, "content": "not json {{"}
    served = list(FOCUS_DESCRIPTORS) + [dup, malformed]

    with mock.patch.dict(os.environ, {"SKCHAT_NOSTR_RELAYS": "wss://relay.preflight"}):
        with mock.patch.object(nostr_mod, "_query_relay", lambda relay, filters: list(served)):
            r = client.get("/sfu/candidates")

    _check(r.status_code == 200, f"/sfu/candidates must be 200, got {r.status_code}")
    body = r.json()
    _check("hosts" in body and isinstance(body["hosts"], list), "response must be {hosts: [...]}")
    hosts = body["hosts"]
    fqids = {h["fqid"] for h in hosts}
    _check(
        fqids == {"opus@chef.skworld", "lumina@chef.skworld", "jarvis@chef.skworld"},
        f"candidate fqids mismatch (dedup/skip): {fqids}",
    )
    for h in hosts:
        _check(
            set(h.keys()) == {"fqid", "auth_url", "sfu_ws_url"},
            f"each host must be {{fqid, auth_url, sfu_ws_url}}: {h}",
        )
    return f"GET /sfu/candidates -> 200 {{hosts: [...]}} ({len(hosts)} hosts, deduped, malformed skipped)"


CHECKS = [
    ("discover_and_elect: oldest-host wins (deterministic)", check_discover_and_elect_oldest_host_wins),
    ("build_signed_assertion: {claim, sig} shape + fresh nonce", check_build_signed_assertion_shape),
    ("get_token: 200 -> POST shape + token", check_get_token_request_shape_200),
    ("get_token: 403 -> AuthDenied", check_get_token_403_auth_denied),
    ("get_token: 500 -> DiscoveryError", check_get_token_500_discovery_error),
    ("GET /sfu/candidates (real route, faked relay) -> shape", check_sfu_candidates_route_shape),
]


GO_LIVE = """
================================ TO GO LIVE ================================
This preflight composed the REAL discovery.py + /sfu/candidates route with
fakes ONLY at the Nostr-relay and SFU-HTTP boundaries. To prove it end-to-end
on real infra (runbooks/cross-host-federation.md):

  1. Start a Space on .158 (the focus host):
       - skchat-webui running on :8765 with SKCHAT_LIVEKIT_* creds set;
       - POST /spaces/create (host_fqid, title, slug) -> note the space_id.
  2. Deploy the SFU on .41 (LiveKit) + run sk-lk-authd (POST /sfu/get) there,
       advertising its focus descriptor (publish_focus) on the shared Nostr relay.
  3. Set SKCHAT_NOSTR_RELAYS=wss://<relay> in BOTH the .158 webui env and the
       .41 discovery client env so /sfu/candidates and discover_and_elect resolve.
  4. Pin jarvis@.41's capauth pubkey in the focus host's federation keystore
       (FULL fqid) so /sfu/get verify_signed succeeds (no impersonation collision).
  5. From .41, run the two-host join from cross-host-federation.md:
       discover_and_elect -> build_signed_assertion -> get_token at the remote
       /sfu/get -> load the .158 webui livekit page with the capped cross-host
       token -> join the room (room == space_id). PASS == browser join observed.
===========================================================================
"""


def main() -> int:
    print("PREFLIGHT: U8 federated-discovery client (local, fakes only at relay + SFU HTTP)\n")
    failures = 0
    for name, fn in CHECKS:
        try:
            detail = fn()
            print(f"  [ok]   {name}\n         -> {detail}")
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            failures += 1
            print(f"  [FAIL] {name}\n         -> {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print()
    if failures:
        print(f"FAIL: {failures}/{len(CHECKS)} preflight checks failed.")
        return 1
    print(f"PASS: all {len(CHECKS)} preflight checks passed.")
    print(GO_LIVE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
