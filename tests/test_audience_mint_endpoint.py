"""Tests for the backend audience-token mint endpoint (POST /api/v1/audience-token).

The SKWorld shell's ``AuthContext.token()`` was stubbed because there was no
backend to mint from. This endpoint closes that gap: an AUTHENTICATED caller
obtains a fresh, short-lived audience-scoped capauth token minted for THIS
daemon's own resolved identity, in the wire form skchat's dataplane accepts.

Two gates, both required:

  * GATE 1 (flag): ``SKCHAT_AUDIENCE_MINT`` (default OFF). When off the route is
    INERT (404, never mints), byte-identical to before this endpoint existed.
  * GATE 2 (auth): the request must carry a valid capauth credential (validated
    via the injectable dataplane validator). An unauthenticated caller gets 401
    and no token is ever minted, even with the flag on.

Anti-forgery: the token subject is resolved server-side from
``capauth.resolve_agent_identity`` (this daemon's own identity). No subject or
agent is read from request input, so an authenticated caller can only ever mint
a token for the daemon it is talking to.

Hermeticity mirrors ``tests/test_dataplane_audience_token.py`` and capauth's own
audience suite: the signature/validity half (``capauth.tokens.verify_token``) is
stubbed so the real audience-match logic and skchat's real wiring (flag gate +
auth gate + mint + export + wire encode) run for real, while no PGP key is
required. A second class does a full end-to-end round trip with a real ephemeral
gpg key, skipped only if a key cannot be generated in the sandbox.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skchat import dataplane_auth, webui


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _FakeValidator:
    """Injectable validator: records calls and returns a fixed verdict."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls = 0

    def validate(self, token: str) -> bool:  # noqa: D401 - mirror CapAuthValidator
        self.calls += 1
        return self.ok


def _agent_home(
    tmp_path: Path, fingerprint: str = "AABBCCDDEE1122334455AABBCCDDEE1122334455"
) -> Path:
    """Create a minimal agent home with an identity file (mirrors capauth tests)."""
    home = tmp_path / ".skcapstone"
    (home / "identity").mkdir(parents=True)
    (home / "security").mkdir(parents=True)
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


def _decode_wire(wire: str):
    """Reverse the endpoint's wire form back into a capauth SignedToken."""
    from capauth import import_token

    padded = wire + "=" * (-len(wire) % 4)
    return import_token(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


@pytest.fixture
def client() -> TestClient:
    return TestClient(webui.app)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    """Every test starts with both flags unset and the default validator."""
    monkeypatch.delenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, raising=False)
    monkeypatch.delenv(dataplane_auth.ENV_FLAG, raising=False)
    dataplane_auth.set_validator(None)
    yield
    dataplane_auth.set_validator(None)


class _StubIdentity:
    fqid = "lumina@chef.skworld"
    uri = "capauth:lumina@skworld.io"


def _install_hermetic_mint(monkeypatch, tmp_path, subject: str = _StubIdentity.fqid):
    """Wire a hermetic, unsigned mint + stubbed verify so no PGP key is needed.

    Returns a dict that records how many times the mint ran and with what
    audience/scopes, so a test can prove the flag-off path never mints and that
    request input never sets the subject.
    """
    from capauth.tokens import AUDIENCE_SCOPES, mint_audience_token

    home = _agent_home(tmp_path)
    monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
    monkeypatch.setattr("capauth.resolve_agent_identity", lambda agent=None: _StubIdentity())

    seen = {"n": 0, "agent": "unset", "audience": None, "scopes": "unset"}

    def _mint(agent=None, audience="skchat", scopes=None, **kwargs):
        seen["n"] += 1
        seen["agent"] = agent
        seen["audience"] = audience
        seen["scopes"] = scopes
        granted = scopes if scopes is not None else AUDIENCE_SCOPES.get(audience, ["chat.send"])
        # subject is fixed server-side (this daemon), never taken from request.
        return mint_audience_token(home, subject, audience, list(granted), sign=False)

    monkeypatch.setattr("capauth.mint_agent_audience_token", _mint)
    return seen


# --------------------------------------------------------------------------- #
# GATE 1: flag off -> inert (404), never mints
# --------------------------------------------------------------------------- #
class TestFlagOff:
    def test_route_inert_and_never_mints_when_flag_off(self, client, monkeypatch, tmp_path):
        seen = _install_hermetic_mint(monkeypatch, tmp_path)
        dataplane_auth.set_validator(_FakeValidator(True))
        # Flag unset (default OFF). Even a fully authenticated request gets 404.
        resp = client.post(
            "/api/v1/audience-token",
            headers={"Authorization": "Bearer valid-cred"},
            json={"audience": "skchat"},
        )
        assert resp.status_code == 404
        assert seen["n"] == 0  # mint was NEVER called

    def test_helper_reports_flag_off(self, monkeypatch):
        monkeypatch.delenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, raising=False)
        assert dataplane_auth.audience_mint_enabled() is False


# --------------------------------------------------------------------------- #
# GATE 2: flag on but unauthenticated -> 401, never mints
# --------------------------------------------------------------------------- #
class TestUnauthenticated:
    def test_no_credential_is_401_and_never_mints(self, client, monkeypatch, tmp_path):
        seen = _install_hermetic_mint(monkeypatch, tmp_path)
        monkeypatch.setenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, "1")
        # No Authorization header at all.
        resp = client.post("/api/v1/audience-token", json={"audience": "skchat"})
        assert resp.status_code == 401
        assert seen["n"] == 0

    def test_invalid_credential_is_401_and_never_mints(self, client, monkeypatch, tmp_path):
        seen = _install_hermetic_mint(monkeypatch, tmp_path)
        monkeypatch.setenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(_FakeValidator(False))  # validator rejects
        resp = client.post(
            "/api/v1/audience-token",
            headers={"Authorization": "Bearer bogus-cred"},
            json={"audience": "skchat"},
        )
        assert resp.status_code == 401
        assert seen["n"] == 0


# --------------------------------------------------------------------------- #
# Happy path: flag on + authenticated -> mints an acceptable token
# --------------------------------------------------------------------------- #
class TestMintHappyPath:
    def test_authenticated_mint_returns_acceptable_token(self, client, monkeypatch, tmp_path):
        seen = _install_hermetic_mint(monkeypatch, tmp_path)
        monkeypatch.setenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(_FakeValidator(True))

        resp = client.post(
            "/api/v1/audience-token",
            headers={"Authorization": "Bearer valid-cred"},
            json={},  # audience defaults to skchat, scopes default to standard set
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["audience"] == "skchat"
        assert body["expires_at"]
        assert seen["n"] == 1

        # The exported/imported form is accepted by skchat's own dataplane check.
        from capauth import verify_audience_token

        tok = _decode_wire(body["token"])
        assert verify_audience_token(tok, "skchat") is True
        # Subject is the daemon's resolved identity, not anything from the request.
        assert tok.payload.subject == _StubIdentity.fqid

    def test_explicit_scopes_passed_through(self, client, monkeypatch, tmp_path):
        seen = _install_hermetic_mint(monkeypatch, tmp_path)
        monkeypatch.setenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(_FakeValidator(True))

        resp = client.post(
            "/api/v1/audience-token",
            headers={"Authorization": "Bearer valid-cred"},
            json={"audience": "skchat", "scopes": ["chat.send"]},
        )
        assert resp.status_code == 200, resp.text
        assert seen["scopes"] == ["chat.send"]
        tok = _decode_wire(resp.json()["token"])
        assert list(tok.payload.capabilities) == ["chat.send"]

    def test_request_cannot_forge_subject_or_agent(self, client, monkeypatch, tmp_path):
        """A subject/agent smuggled into the body is ignored (anti-forgery)."""
        seen = _install_hermetic_mint(monkeypatch, tmp_path)
        monkeypatch.setenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(_FakeValidator(True))

        resp = client.post(
            "/api/v1/audience-token",
            headers={"Authorization": "Bearer valid-cred"},
            json={"audience": "skchat", "agent": "victim", "subject": "victim@evil"},
        )
        assert resp.status_code == 200, resp.text
        # The route always mints for its own identity: agent=None to capauth, and
        # the resolved subject stands, never the caller-supplied "victim".
        assert seen["agent"] is None
        tok = _decode_wire(resp.json()["token"])
        assert tok.payload.subject == _StubIdentity.fqid
        assert tok.payload.subject != "victim@evil"

    def test_mint_error_fails_closed_500(self, client, monkeypatch, tmp_path):
        _install_hermetic_mint(monkeypatch, tmp_path)
        monkeypatch.setenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(_FakeValidator(True))

        def _boom(*a, **k):
            raise RuntimeError("signing backend down")

        monkeypatch.setattr("capauth.mint_agent_audience_token", _boom)
        resp = client.post(
            "/api/v1/audience-token",
            headers={"Authorization": "Bearer valid-cred"},
            json={},
        )
        assert resp.status_code == 500
        assert "token" not in resp.json()


# --------------------------------------------------------------------------- #
# Full end-to-end with a real ephemeral gpg signature (no stubbing of the mint)
# --------------------------------------------------------------------------- #
def _os_environ() -> dict:
    import os

    return dict(os.environ)


def _gen_gpg_key(gnupghome: Path) -> str | None:
    if not shutil.which("gpg"):
        return None
    gnupghome.mkdir(parents=True, exist_ok=True)
    gnupghome.chmod(0o700)
    env = {"GNUPGHOME": str(gnupghome)}
    try:
        subprocess.run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--passphrase",
                "",
                "--pinentry-mode",
                "loopback",
                "--quick-generate-key",
                "skchat-mint-test@local",
                "ed25519",
                "sign",
                "0",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**_os_environ(), **env},
        )
        out = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--list-secret-keys"],
            capture_output=True,
            text=True,
            timeout=15,
            env={**_os_environ(), **env},
        )
        for line in out.stdout.splitlines():
            if line.startswith("fpr:"):
                return line.split(":")[9]
    except (subprocess.SubprocessError, OSError):
        return None
    return None


class TestMintRealSignature:
    def test_real_signed_token_round_trips(self, client, monkeypatch, tmp_path):
        gnupghome = tmp_path / "gnupg"
        fp = _gen_gpg_key(gnupghome)
        if not fp:
            pytest.skip("gpg key generation unavailable in this sandbox")
        monkeypatch.setenv("GNUPGHOME", str(gnupghome))

        home = _agent_home(tmp_path, fingerprint=fp)
        subject = "lumina@chef.skworld"
        _ident = type("I", (), {"fqid": subject, "uri": "capauth:lumina@skworld.io"})()
        monkeypatch.setattr("capauth.resolve_capauth_home", lambda: home)
        # Subject is server-derived. mint_agent_audience_token resolves it via the
        # tokens-module reference; the route logs via the top-level one. Patch both.
        monkeypatch.setattr("capauth.resolve_agent_identity", lambda agent=None: _ident)
        monkeypatch.setattr("capauth.tokens.resolve_agent_identity", lambda agent=None: _ident)

        monkeypatch.setenv(dataplane_auth.AUDIENCE_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(_FakeValidator(True))

        resp = client.post(
            "/api/v1/audience-token",
            headers={"Authorization": "Bearer valid-cred"},
            json={"audience": "skchat", "scopes": ["chat.send"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        from capauth import verify_audience_token

        tok = _decode_wire(body["token"])
        assert tok.signature, "token should have been really signed"
        assert verify_audience_token(tok, "skchat") is True  # real signature + audience
        assert verify_audience_token(tok, "skcode") is False  # wrong audience
        assert tok.payload.subject == subject
