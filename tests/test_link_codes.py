"""Short-lived, single-use codes for linking a device.

The operator token is a long-lived shared secret sitting in plaintext env files,
with no generator and no rotation path. It is also, today, the thing the operator
types into a phone to link it. That is the wrong shape for a bootstrap
credential: a secret you paste into clients should be short-lived and single-use,
and a long-lived service credential should never leave the box at all.

A link code is that bootstrap credential. It is minted on the box (shell access
already implies control), expires in minutes, and burns on first use.

Two properties are load-bearing:

  * **Only enrollment accepts it.** ``_require_operator`` guards guest invites,
    prekey signing and call routes too. A code that opened those would be a
    strictly worse operator token, not a better one. It is accepted ONLY where a
    device links itself.
  * **Only a hash is stored.** The plaintext exists in the operator's terminal
    and nowhere else, so a readable state file does not hand someone the code.

Presented in the SAME header the operator token uses, deliberately: the app
already has a paste field wired to that header, so a short-lived code drops
straight into the existing flow with no client change at all.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from skchat import link_codes as LC


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LINK_CODES", str(tmp_path / "link_codes.json"))
    yield


def test_a_freshly_minted_code_verifies():
    code = LC.mint()
    assert LC.verify(code) is True


def test_a_code_burns_on_first_use():
    """Single-use: the second attempt is refused even seconds later."""
    code = LC.mint()
    assert LC.verify(code) is True
    assert LC.verify(code) is False, "a link code must not be reusable"


def test_an_expired_code_is_refused():
    code = LC.mint(ttl=60, now=1000.0)
    assert LC.verify(code, now=1000.0 + 59) is True

    code2 = LC.mint(ttl=60, now=1000.0)
    assert LC.verify(code2, now=1000.0 + 61) is False


def test_a_wrong_code_is_refused():
    LC.mint()
    assert LC.verify("not-the-code") is False
    assert LC.verify("") is False


def test_the_plaintext_code_is_never_written_to_disk():
    """A readable state file must not hand anyone a working code."""
    code = LC.mint()
    raw = LC.store_path().read_text()

    assert code not in raw, "the code itself must never be persisted"
    assert "hash" in raw or "h" in json.loads(raw)[0]


def test_several_codes_can_be_outstanding_and_each_burns_once():
    a, b = LC.mint(), LC.mint()

    assert LC.verify(a) is True
    assert LC.verify(b) is True
    assert LC.verify(a) is False
    assert LC.verify(b) is False


def test_expired_entries_are_pruned_so_the_file_cannot_grow_forever():
    for _ in range(5):
        LC.mint(ttl=1, now=1000.0)
    LC.mint(ttl=600, now=5000.0)  # a later mint prunes the dead ones

    assert len(json.loads(LC.store_path().read_text())) == 1


def test_a_corrupt_store_refuses_rather_than_admitting_anything():
    """Fail closed: an unreadable store must never mean "accept any code"."""
    LC.store_path().parent.mkdir(parents=True, exist_ok=True)
    LC.store_path().write_text("{not json")

    assert LC.verify("anything") is False


def test_a_code_is_long_enough_to_resist_guessing():
    code = LC.mint()
    # It is typed by a human, so it is not 43 chars, but it must still be well
    # beyond guessable for something that opens enrollment for its lifetime.
    assert len(code.replace("-", "")) >= 12


def test_codes_are_unique():
    assert len({LC.mint() for _ in range(20)}) == 20


def test_revoke_all_kills_outstanding_codes():
    a, b = LC.mint(), LC.mint()
    LC.revoke_all()

    assert LC.verify(a) is False
    assert LC.verify(b) is False


def test_time_actually_passes_by_default():
    """The default now= path must use the clock, not a frozen value."""
    code = LC.mint(ttl=0)
    time.sleep(0.01)
    assert LC.verify(code) is False


# ── The route wiring, driven for real ──────────────────────────────────────── #
# The unit tests above cover the code's own lifecycle. These cover the property
# that actually keeps it from being a worse operator token: it opens enrollment
# and NOTHING else.


@pytest.fixture
def enroll_client(tmp_path, monkeypatch):
    from skchat import operator_auth as OA
    from skchat.operator_auth_routes import register_operator_auth_routes

    monkeypatch.setenv("SKCHAT_LINK_CODES", str(tmp_path / "link_codes.json"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    # With a shared token SET, _require_operator stops falling back to loopback,
    # so a caller presenting neither is genuinely refused. Without this the
    # loopback fallback would let every request through and prove nothing.
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "the-real-operator-token")
    app = FastAPI()
    register_operator_auth_routes(app, device_store=OA.DeviceStore(tmp_path / "devices.json"))
    return TestClient(app, client=("203.0.113.9", 4321))  # not loopback


def test_a_link_code_opens_an_enrollment_window(enroll_client):
    code = LC.mint()
    r = enroll_client.post("/api/v1/auth/enroll/open", headers={"X-Operator-Token": code})
    assert r.status_code == 200, r.text
    assert r.json()["window_nonce"]


def test_the_real_operator_token_still_opens_one(enroll_client):
    r = enroll_client.post(
        "/api/v1/auth/enroll/open",
        headers={"X-Operator-Token": "the-real-operator-token"},
    )
    assert r.status_code == 200, r.text


def test_a_caller_with_neither_is_refused(enroll_client):
    r = enroll_client.post("/api/v1/auth/enroll/open")
    assert r.status_code in (401, 403)


def test_a_link_code_is_spent_by_opening_a_window(enroll_client):
    code = LC.mint()
    assert (
        enroll_client.post(
            "/api/v1/auth/enroll/open", headers={"X-Operator-Token": code}
        ).status_code
        == 200
    )
    second = enroll_client.post("/api/v1/auth/enroll/open", headers={"X-Operator-Token": code})
    assert second.status_code in (401, 403), "a link code must not open a second window"


def test_an_expired_link_code_does_not_open_a_window(enroll_client):
    code = LC.mint(ttl=0)
    r = enroll_client.post("/api/v1/auth/enroll/open", headers={"X-Operator-Token": code})
    assert r.status_code in (401, 403)


def test_a_link_code_does_NOT_unlock_the_other_operator_routes(tmp_path, monkeypatch):
    """The property that stops this being a worse operator token.

    _require_operator also guards guest invites, prekey signing and the call
    routes. A link code must not reach any of them.
    """
    from skchat.guest import _require_operator

    monkeypatch.setenv("SKCHAT_LINK_CODES", str(tmp_path / "link_codes.json"))
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "the-real-operator-token")

    app = FastAPI()

    @app.post("/some-other-operator-route")
    async def other(request: Request):
        _require_operator(request)  # the gate every other operator route uses
        return {"ok": True}

    client = TestClient(app, client=("203.0.113.9", 4321))
    code = LC.mint()

    refused = client.post("/some-other-operator-route", headers={"X-Operator-Token": code})
    assert refused.status_code in (401, 403), (
        "a link code must open enrollment ONLY, never the wider operator surface"
    )
    assert LC.verify(code) is True, "and being refused must not have burned it"
