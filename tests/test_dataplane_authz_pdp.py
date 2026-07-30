"""M3 authz PEP: SKCHAT_AUTHZ_PDP off | shadow | enforce over the dataplane gate.

Authentication is unchanged; the PDP layers on. off = authenticate only; shadow =
compute the PDP decision, log divergence, return the LEGACY outcome (no behavior
change); enforce = the PDP decision governs (auth must still pass first).
"""

from __future__ import annotations

import types

import pytest
from fastapi import HTTPException, Request

from skchat import dataplane_auth


class _Stub:
    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def validate(self, token: str) -> bool:
        return self.ok


@pytest.fixture(autouse=True)
def _reset():
    yield
    dataplane_auth.set_validator(None)


def _req(path: str = "/api/send") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"authorization", b"Bearer tok")],
        }
    )


def _decision(allow: bool):
    return types.SimpleNamespace(allow=allow, reason="x", obligations=[])


def test_capability_mapping():
    assert dataplane_auth._capability_for_path("/api/send") == "skchat.send"
    assert dataplane_auth._capability_for_path("/api/v1/inbox") == "skchat.inbox"
    assert dataplane_auth._capability_for_path("/api/v1/prekey") == "skchat.prekey"
    assert dataplane_auth._capability_for_path("/other") is None


def test_pdp_mode_parsing(monkeypatch):
    monkeypatch.delenv("SKCHAT_AUTHZ_PDP", raising=False)
    assert dataplane_auth.authz_pdp_mode() == "off"
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "shadow")
    assert dataplane_auth.authz_pdp_mode() == "shadow"
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "enforce")
    assert dataplane_auth.authz_pdp_mode() == "enforce"
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "garbage")
    assert dataplane_auth.authz_pdp_mode() == "off"


def test_off_mode_never_calls_pdp(monkeypatch):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.delenv("SKCHAT_AUTHZ_PDP", raising=False)
    dataplane_auth.set_validator(_Stub(True))
    called = []
    monkeypatch.setattr("capauth.authz.decide", lambda *a, **k: called.append(1) or _decision(False))
    dataplane_auth.enforce_dataplane_auth(_req())  # auth ok -> proceeds
    assert called == []  # PDP never consulted when off


def test_shadow_returns_legacy_even_when_pdp_denies(monkeypatch):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "shadow")
    dataplane_auth.set_validator(_Stub(True))
    monkeypatch.setattr(dataplane_auth, "_extract_subject", lambda t: "lumina@host")
    monkeypatch.setattr("capauth.authz.decide", lambda *a, **k: _decision(False))
    # PDP denies, but shadow returns the legacy outcome (auth ok) -> no raise.
    dataplane_auth.enforce_dataplane_auth(_req())


def test_shadow_still_401s_on_auth_failure(monkeypatch):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "shadow")
    dataplane_auth.set_validator(_Stub(False))
    with pytest.raises(HTTPException) as ei:
        dataplane_auth.enforce_dataplane_auth(_req())
    assert ei.value.status_code == 401


def test_enforce_denies_when_pdp_denies(monkeypatch):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "enforce")
    dataplane_auth.set_validator(_Stub(True))
    monkeypatch.setattr(dataplane_auth, "_extract_subject", lambda t: "lumina@host")
    monkeypatch.setattr("capauth.authz.decide", lambda *a, **k: _decision(False))
    with pytest.raises(HTTPException) as ei:
        dataplane_auth.enforce_dataplane_auth(_req())
    assert ei.value.status_code == 403  # authenticated but not authorized


def test_enforce_allows_when_pdp_allows(monkeypatch):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "enforce")
    dataplane_auth.set_validator(_Stub(True))
    monkeypatch.setattr(dataplane_auth, "_extract_subject", lambda t: "lumina@host")
    monkeypatch.setattr("capauth.authz.decide", lambda *a, **k: _decision(True))
    dataplane_auth.enforce_dataplane_auth(_req())  # no raise


def test_enforce_requires_auth_first(monkeypatch):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "enforce")
    dataplane_auth.set_validator(_Stub(False))  # authentication fails
    with pytest.raises(HTTPException) as ei:
        dataplane_auth.enforce_dataplane_auth(_req())
    assert ei.value.status_code == 401  # 401 before any authz consideration
