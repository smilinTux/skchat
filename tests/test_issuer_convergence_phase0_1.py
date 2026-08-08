"""CR-3.4 Phase 0 + Phase 1: issuer convergence (additive, HS256 untouched).

Covers, all shadow/parallel-safe and gated OFF by default:

  * P1  - ``_extract_subject`` resolves a valid skchat-audience token to its
    payload subject (``operator:<fp>`` for the seat, the fqid for a daemon-self
    token), tried AFTER the operator-session branch and BEFORE the FQID branch,
    gated on ``SKCHAT_ACCEPT_AUDIENCE_TOKENS``.
  * P3  - the token-mint gates require a PRIMARY credential: an audience token
    authenticates but can NEVER mint another (no renewal laundering).
  * AC1 - the operator session handshake ALSO mints a parallel operator-audience
    token (``SKCHAT_OPERATOR_AUDIENCE_ISSUE``, default OFF, non-fatal), additive.
  * P5  - the server-side issuer shadow compares the audience path against the
    live HS256 session on a synthetic twin, logging divergence, never changing
    the response (``SKCHAT_ISSUER_SHADOW``, default OFF).
  * HS256 operator sessions are proven UNCHANGED throughout.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import dataplane_auth


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _agent_home(
    tmp_path: Path, fingerprint: str = "AABBCCDDEE1122334455AABBCCDDEE1122334455"
) -> Path:
    home = tmp_path / ".skcapstone"
    (home / "identity").mkdir(parents=True, exist_ok=True)
    (home / "security").mkdir(parents=True, exist_ok=True)
    (home / "identity" / "identity.json").write_text(
        json.dumps(
            {
                "name": "TestAgent",
                "email": "test@skcapstone.local",
                "fingerprint": fingerprint,
                "capauth_managed": True,
            }
        )
    )
    return home


def _wire(token) -> str:
    from capauth.tokens import export_token

    return (
        base64.urlsafe_b64encode(export_token(token).encode("utf-8")).decode("ascii").rstrip("=")
    )


class _FakeHeaders(dict):
    def get(self, key, default=None):  # case-insensitive like starlette
        return super().get(key.lower(), default)


class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeRequest:
    def __init__(
        self, headers=None, method: str = "POST", path: str = "/api/v1/audience-token"
    ) -> None:
        self.headers = _FakeHeaders({k.lower(): v for k, v in (headers or {}).items()})
        self.method = method
        self.url = _FakeURL(path)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for flag in (
        dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG,
        dataplane_auth.ISSUER_SHADOW_ENV_FLAG,
        dataplane_auth.AUDIENCE_MINT_ENV_FLAG,
        dataplane_auth.ENV_FLAG,
        "SKCHAT_OPERATOR_AUDIENCE_ISSUE",
        "SKCHAT_EMBED_TOKENS",
    ):
        monkeypatch.delenv(flag, raising=False)
    dataplane_auth.set_validator(None)
    dataplane_auth._shadow_twins.clear()
    yield
    dataplane_auth.set_validator(None)
    dataplane_auth._shadow_twins.clear()


# --------------------------------------------------------------------------- #
# P1: audience-token subject resolution
# --------------------------------------------------------------------------- #
class TestExtractSubjectAudienceBranch:
    def test_operator_audience_token_resolves_to_operator_subject(self, tmp_path, monkeypatch):
        from capauth.tokens import mint_audience_token

        home = _agent_home(tmp_path)
        tok = mint_audience_token(
            home=home,
            subject="operator:abc123def456",
            audience="skchat",
            scopes=["chat.read", "chat.send"],
            sign=False,
        )
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        assert dataplane_auth._extract_subject(_wire(tok)) == "operator:abc123def456"

    def test_daemon_self_fqid_subject_resolves(self, tmp_path, monkeypatch):
        from capauth.tokens import mint_audience_token

        home = _agent_home(tmp_path)
        tok = mint_audience_token(
            home=home,
            subject="lumina@chef.skworld",
            audience="skchat",
            scopes=["chat.send"],
            sign=False,
        )
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        assert dataplane_auth._extract_subject(_wire(tok)) == "lumina@chef.skworld"

    def test_audience_branch_inert_when_flag_off(self, tmp_path, monkeypatch):
        """Flag OFF: the audience branch is never consulted -> None (byte-identical)."""
        from capauth.tokens import mint_audience_token

        home = _agent_home(tmp_path)
        tok = mint_audience_token(
            home=home,
            subject="operator:abc123",
            audience="skchat",
            scopes=["chat.send"],
            sign=False,
        )
        called = {"n": 0}

        def _boom(*a, **k):  # pragma: no cover - must not run when flag off
            called["n"] += 1
            return True

        monkeypatch.setattr("capauth.verify_audience_token", _boom)
        assert dataplane_auth._extract_subject(_wire(tok)) is None
        assert called["n"] == 0

    def test_wrong_audience_not_resolved(self, tmp_path, monkeypatch):
        from capauth.tokens import mint_audience_token

        home = _agent_home(tmp_path)
        tok = mint_audience_token(
            home=home,
            subject="operator:abc123",
            audience="skcode",
            scopes=["skcode.stream"],
            sign=False,
        )
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        assert dataplane_auth._extract_subject(_wire(tok)) is None


# --------------------------------------------------------------------------- #
# HS256 UNCHANGED
# --------------------------------------------------------------------------- #
class TestHS256Unchanged:
    def test_legacy_session_authenticates_and_resolves(self, monkeypatch):
        from skchat import operator_auth as oa

        monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "operator-secret-value")
        token = oa.mint_operator_session(device_fp="fpLEGACY01", ttl=60)
        assert dataplane_auth.CapAuthValidator().validate(token) is True
        assert dataplane_auth._extract_subject(token) == "operator:fpLEGACY01"

    def test_hs256_resolution_precedes_audience_even_with_flag_on(self, monkeypatch):
        from skchat import operator_auth as oa

        monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "operator-secret-value")
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        token = oa.mint_operator_session(device_fp="fpFIRST22", ttl=60)
        # First branch (HS256) wins; audience branch never consulted for a JWT.
        assert dataplane_auth._extract_subject(token) == "operator:fpFIRST22"


# --------------------------------------------------------------------------- #
# P3: primary-credential rule (no renewal laundering)
# --------------------------------------------------------------------------- #
class TestPrimaryCredentialRule:
    def _audience_wire(self, tmp_path, monkeypatch):
        from capauth.tokens import mint_audience_token

        home = _agent_home(tmp_path)
        tok = mint_audience_token(
            home=home,
            subject="operator:abc123",
            audience="skchat",
            scopes=["chat.send"],
            sign=False,
        )
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        return _wire(tok)

    def test_helpers_split_authenticated_vs_primary(self, tmp_path, monkeypatch):
        wire = self._audience_wire(tmp_path, monkeypatch)
        dataplane_auth.set_validator(None)  # real default validator
        req = _FakeRequest(headers={"Authorization": "Bearer " + wire})
        # The audience token AUTHENTICATES (accept flag on) ...
        assert dataplane_auth.request_is_authenticated(req) is True
        # ... but is NOT a primary credential (no laundering).
        assert dataplane_auth.request_is_primary_authenticated(req) is False
        assert dataplane_auth._credential_is_audience_token(wire) is True

    def test_operator_session_is_primary(self, monkeypatch):
        from skchat import operator_auth as oa

        monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "operator-secret-value")
        dataplane_auth.set_validator(None)
        token = oa.mint_operator_session(device_fp="fpPRIM01", ttl=60)
        req = _FakeRequest(headers={"Authorization": "Bearer " + token})
        assert dataplane_auth.request_is_primary_authenticated(req) is True
        assert dataplane_auth._credential_is_audience_token(token) is False

    def test_audience_token_refused_at_audience_mint(self, tmp_path, monkeypatch):
        wire = self._audience_wire(tmp_path, monkeypatch)
        monkeypatch.setenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(None)
        from skchat import webui

        client = TestClient(webui.app)
        resp = client.post(
            "/api/v1/audience-token",
            headers={"Authorization": "Bearer " + wire},
            json={},
        )
        # Authenticated (would be 200 under the old request_is_authenticated gate),
        # but refused now: an audience token is not primary.
        assert resp.status_code == 401

    def test_audience_token_refused_at_embed_mint(self, tmp_path, monkeypatch):
        wire = self._audience_wire(tmp_path, monkeypatch)
        monkeypatch.setenv("SKCHAT_EMBED_TOKENS", "1")
        dataplane_auth.set_validator(None)
        from skchat import webui

        client = TestClient(webui.app)
        resp = client.post(
            "/api/v1/embed-token",
            headers={"Authorization": "Bearer " + wire},
            json={"module": "skdashboard"},
        )
        assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# P1 + PDP: a minted operator-audience token resolves AND authorizes
# --------------------------------------------------------------------------- #
class TestOperatorAudienceAuthorizes:
    def test_resolves_to_operator_subject_and_authorizes(self, tmp_path, monkeypatch):
        # capauth default_base_dir() and resolve_capauth_home() both root at
        # Path.home(); pin it to tmp so the grant + decide + mint stay hermetic.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        (tmp_path / ".skcapstone").mkdir(parents=True, exist_ok=True)

        from skchat.dataplane_auth import operator_subject
        from skchat.operator_audience import mint_operator_audience_token
        from skchat.operator_grants import grant_operator_capabilities

        device_fp = "abc123deadbeef01"
        pubkey_b64 = "TESTPUBKEYb64AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        assert grant_operator_capabilities(device_fp, pubkey_b64) is True
        subject = operator_subject(device_fp)

        tok = mint_operator_audience_token(device_fp)
        assert tok.payload.subject == subject
        assert tok.payload.audience == "skchat"
        assert tok.payload.metadata.get("tier") == "operator-session"

        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        assert dataplane_auth._extract_subject(_wire(tok)) == subject

        # Authorizes under the live rules: the granted operator holds skchat.inbox.
        from capauth.authz import decide

        assert decide(subject, "skchat.inbox").allow is True


# --------------------------------------------------------------------------- #
# AC1 / Phase 1: parallel operator-audience issuance at the session handshake
# --------------------------------------------------------------------------- #
def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _kp():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return priv, base64.b64encode(spki).decode()


def _sig(priv, payload):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    der = priv.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


@pytest.fixture
def session_client(tmp_path, monkeypatch):
    from skchat import operator_auth as oa
    from skchat.operator_auth_routes import register_operator_auth_routes

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "operator-secret-value")
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    app = FastAPI()
    register_operator_auth_routes(app, device_store=oa.DeviceStore(tmp_path / "d.json"))
    return TestClient(app, client=("127.0.0.1", 12345))


def _enroll_and_fp(client):
    priv, pub = _kp()
    w = client.post("/api/v1/auth/enroll/open").json()
    sig = _sig(priv, _canon({"nonce": w["window_nonce"], "device_pubkey": pub}))
    e = client.post(
        "/api/v1/auth/enroll",
        json={"device_pubkey": pub, "window_nonce": w["window_nonce"], "sig": sig},
    )
    assert e.status_code == 200
    fp = e.json()["device_fp"]
    # Phase 3: a fresh fp lands pending and cannot mint a session. This suite is
    # about the dual audience-token mint, not the approval gate itself (see
    # test_device_approval.py), so approve the way an operator would.
    from skchat import device_registry as DR

    DR.set_approved(fp, True)
    return priv, pub, fp


def _open_session(client, priv, fp):
    ch = client.get("/api/v1/auth/challenge").json()
    ssig = _sig(priv, _canon({"nonce": ch["nonce"], "device_fp": fp}))
    return client.post(
        "/api/v1/auth/session", json={"device_fp": fp, "nonce": ch["nonce"], "sig": ssig}
    )


class TestSessionDualMint:
    def test_no_audience_token_when_flag_off(self, session_client, monkeypatch):
        priv, _pub, fp = _enroll_and_fp(session_client)
        # SKCHAT_OPERATOR_AUDIENCE_ISSUE unset (default OFF).
        r = _open_session(session_client, priv, fp)
        assert r.status_code == 200
        body = r.json()
        assert body["session_token"]  # HS256 session unchanged
        assert "audience_token" not in body  # additive field absent when flag off

    def test_dual_mints_audience_token_when_flag_on(self, session_client, monkeypatch):
        priv, _pub, fp = _enroll_and_fp(session_client)
        monkeypatch.setenv("SKCHAT_OPERATOR_AUDIENCE_ISSUE", "1")
        r = _open_session(session_client, priv, fp)
        assert r.status_code == 200
        body = r.json()
        assert body["session_token"]  # HS256 stays primary
        assert body["audience_token"]  # additive audience token present
        assert body["audience_expires_at"]
        # The audience token resolves to the same operator subject.
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        assert dataplane_auth._extract_subject(body["audience_token"]) == f"operator:{fp}"

    def test_mint_failure_is_non_fatal(self, session_client, monkeypatch):
        priv, _pub, fp = _enroll_and_fp(session_client)
        monkeypatch.setenv("SKCHAT_OPERATOR_AUDIENCE_ISSUE", "1")

        def _boom(device_fp):
            raise RuntimeError("capauth keyring down")

        monkeypatch.setattr("skchat.operator_audience.mint_operator_audience_token", _boom)
        r = _open_session(session_client, priv, fp)
        # The HS256 session still returns; the seat is never locked out.
        assert r.status_code == 200
        body = r.json()
        assert body["session_token"]
        assert "audience_token" not in body


# --------------------------------------------------------------------------- #
# P5 / Phase 1: server-side issuer shadow (observation only)
# --------------------------------------------------------------------------- #
class TestIssuerShadow:
    def _hs_and_twin(self, tmp_path, monkeypatch, twin_subject: str):
        from capauth.tokens import mint_audience_token

        from skchat import operator_auth as oa

        monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "operator-secret-value")
        hs = oa.mint_operator_session(device_fp="fpSHADOW1", ttl=60)
        home = _agent_home(tmp_path)
        twin = mint_audience_token(
            home=home,
            subject=twin_subject,
            audience="skchat",
            scopes=["chat.send"],
            sign=False,
        )
        monkeypatch.setattr(
            "skchat.operator_audience.mint_operator_audience_token", lambda fp: twin
        )
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        return hs

    def test_no_divergence_when_subjects_match(self, tmp_path, monkeypatch, caplog):
        hs = self._hs_and_twin(tmp_path, monkeypatch, "operator:fpSHADOW1")
        req = _FakeRequest(method="GET", path="/health")  # capability None: subject-only
        with caplog.at_level("INFO", logger="skchat.dataplane_auth"):
            dataplane_auth._issuer_shadow_compare(req, hs)
        assert "issuer-shadow divergence" not in caplog.text

    def test_logs_divergence_on_subject_mismatch(self, tmp_path, monkeypatch, caplog):
        hs = self._hs_and_twin(tmp_path, monkeypatch, "operator:WRONGFP")
        req = _FakeRequest(method="GET", path="/health")
        with caplog.at_level("WARNING", logger="skchat.dataplane_auth"):
            dataplane_auth._issuer_shadow_compare(req, hs)
        assert "issuer-shadow divergence" in caplog.text

    def test_never_raises_on_twin_mint_failure(self, tmp_path, monkeypatch):
        from skchat import operator_auth as oa

        monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "operator-secret-value")
        hs = oa.mint_operator_session(device_fp="fpSHADOW2", ttl=60)

        def _boom(device_fp):
            raise RuntimeError("keyring down")

        monkeypatch.setattr("skchat.operator_audience.mint_operator_audience_token", _boom)
        req = _FakeRequest(method="GET", path="/health")
        # Observation only: must not raise into the request path.
        dataplane_auth._issuer_shadow_compare(req, hs)

    def test_non_operator_credential_is_ignored(self, monkeypatch):
        # A non-HS256-operator-session credential: the shadow simply returns.
        req = _FakeRequest(method="GET", path="/health")
        dataplane_auth._issuer_shadow_compare(req, "not-an-operator-session")
