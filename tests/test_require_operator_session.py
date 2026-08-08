"""_require_operator accepts an enrolled-operator SESSION Bearer, not only the
raw shared secret.

Regression for the prekey-sign 401: the Flutter app authenticates every call
with its operator-session JWT (via the data-plane auth interceptor), never the
raw ``SKCHAT_GUEST_OPERATOR_TOKEN`` shared secret. Before the fix,
``POST /api/v1/prekey/sign`` 401'd for the real operator because
``_require_operator`` compared the session JWT against the shared secret and
missed. It must now accept a valid operator-tier session, while still rejecting
a guest/peer credential (the signing-oracle invariant).
"""

import pytest

from skchat import guest
from skchat import operator_auth as oa


class _Req:
    """Minimal stand-in for a Starlette Request: headers + a private client."""

    def __init__(self, headers: dict, host: str = "1.2.3.4"):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.client = type("C", (), {"host": host})()


def _bearer(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def test_valid_operator_session_bearer_is_accepted(monkeypatch):
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv(guest._OPERATOR_TOKEN_ENV, "the-shared-secret")
    session = oa.mint_operator_session(device_fp="abc123", ttl=60)

    # A remote (non-private) client presenting only the session JWT must pass.
    guest._require_operator(_Req(_bearer(session), host="203.0.113.9"))


def test_exact_shared_secret_still_accepted(monkeypatch):
    monkeypatch.setenv(guest._OPERATOR_TOKEN_ENV, "the-shared-secret")
    guest._require_operator(_Req(_bearer("the-shared-secret"), host="203.0.113.9"))
    # X-Operator-Token header form too.
    guest._require_operator(_Req({"X-Operator-Token": "the-shared-secret"}, host="203.0.113.9"))


def test_random_bearer_is_rejected(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv(guest._OPERATOR_TOKEN_ENV, "the-shared-secret")
    with pytest.raises(HTTPException) as ei:
        guest._require_operator(_Req(_bearer("not-a-real-token"), host="203.0.113.9"))
    assert ei.value.status_code == 401


def test_session_minted_under_a_different_secret_is_rejected(monkeypatch):
    """A JWT that is not a valid operator session under THIS secret is refused,
    so the acceptance path cannot be spoofed by a foreign-signed token."""
    from fastapi import HTTPException

    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "secret-a")
    foreign = oa.mint_operator_session(device_fp="abc123", ttl=60)
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "secret-b")
    monkeypatch.setenv(guest._OPERATOR_TOKEN_ENV, "the-shared-secret")
    with pytest.raises(HTTPException) as ei:
        guest._require_operator(_Req(_bearer(foreign), host="203.0.113.9"))
    assert ei.value.status_code == 401
