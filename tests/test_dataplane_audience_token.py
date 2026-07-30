"""Tests for the third data-plane credential path: capauth audience tokens.

``skchat.dataplane_auth`` accepts, as a THIRD credential (after the operator-
session JWT and the base64url {claim, sig} FQID assertion), a capauth
audience-scoped token minted for the ``skchat`` audience. The path is gated by
``SKCHAT_ACCEPT_AUDIENCE_TOKENS`` (default OFF):

  * flag OFF (default) -> the audience path is never consulted; an otherwise-
    valid audience token is NOT accepted (behavior byte-identical to before).
  * flag ON  -> a valid skchat-audience token is accepted; a wrong-audience or
    unscoped token is rejected; garbage fails closed.

The wire form is the base64url of ``capauth.export_token(token)`` JSON (a
whitespace-free credential that rides in an Authorization / X-CapAuth-Token
header). skchat reconstructs the token via ``capauth.import_token`` and accepts
it only when ``capauth.verify_audience_token(t, "skchat")`` affirms it.

Two acceptance strategies are exercised:
  * ``TestAudiencePathMocked`` isolates the audience gate exactly as capauth's own
    ``tests/test_audience_tokens.py`` does: it stubs the signature/validity half
    (``capauth.tokens.verify_token``) so the real audience-match logic and skchat's
    real wiring (flag gate + wire decode + import_token + verify_audience_token)
    run for real, while the PGP signature is not required.
  * ``TestAudiencePathRealSignature`` does a full end-to-end round trip with a real
    ephemeral gpg key (no stubbing at all), and is skipped only if a gpg key
    cannot be generated in the sandbox.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from capauth.tokens import export_token, issue_token, mint_audience_token

from skchat import dataplane_auth


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _wire(token) -> str:
    """Encode a SignedToken into the credential wire form (base64url of JSON)."""
    raw = export_token(token).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _agent_home(tmp_path: Path, fingerprint: str = "AABBCCDDEE1122334455AABBCCDDEE1122334455") -> Path:
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


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    """Every test starts with the audience flag unset (default OFF)."""
    monkeypatch.delenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, raising=False)
    yield


# --------------------------------------------------------------------------- #
# Audience path with the signature half stubbed (mirrors capauth's own suite)
# --------------------------------------------------------------------------- #
class TestAudiencePathMocked:
    def _validate(self, cred: str) -> bool:
        return dataplane_auth.CapAuthValidator().validate(cred)

    def test_valid_skchat_token_accepted_when_flag_on(self, tmp_path, monkeypatch):
        home = _agent_home(tmp_path)
        token = mint_audience_token(
            home=home, subject="chef-session", audience="skchat",
            scopes=["chat.send"], sign=False,
        )
        # Isolate the audience gate: stub the signature/validity half (as
        # capauth/tests/test_audience_tokens.py does) so the real audience-match
        # logic and skchat's real wiring run.
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        assert self._validate(_wire(token)) is True

    def test_wrong_audience_rejected_when_flag_on(self, tmp_path, monkeypatch):
        home = _agent_home(tmp_path)
        token = mint_audience_token(
            home=home, subject="s", audience="skcode",
            scopes=["skcode.stream"], sign=False,
        )
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        # Audience mismatch is rejected even though the signature half would pass.
        assert self._validate(_wire(token)) is False

    def test_unscoped_token_rejected_when_flag_on(self, tmp_path, monkeypatch):
        home = _agent_home(tmp_path)
        token = issue_token(
            home=home, subject="s", capabilities=["chat.send"], sign=False,
        )
        assert token.payload.audience is None
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        # Unscoped (audience=None) is never accepted via the audience path.
        assert self._validate(_wire(token)) is False

    def test_garbage_credential_fails_closed_when_flag_on(self, monkeypatch):
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        # Not base64url of a capauth token -> fails closed, no exception escapes.
        assert self._validate("this-is-not-a-token") is False
        assert self._validate(base64.urlsafe_b64encode(b'{"foo":1}').decode()) is False

    def test_valid_token_not_accepted_when_flag_off(self, tmp_path, monkeypatch):
        home = _agent_home(tmp_path)
        token = mint_audience_token(
            home=home, subject="s", audience="skchat",
            scopes=["chat.send"], sign=False,
        )
        # Signature half would pass, flag is OFF -> the audience path is never
        # consulted, so the otherwise-valid audience token is NOT accepted.
        monkeypatch.setattr("capauth.tokens.verify_token", lambda t, h=None: True)
        assert dataplane_auth.accept_audience_tokens() is False
        assert self._validate(_wire(token)) is False

    def test_audience_path_not_consulted_when_flag_off(self, tmp_path, monkeypatch):
        """With the flag off, verify_audience_token is never even called."""
        home = _agent_home(tmp_path)
        token = mint_audience_token(
            home=home, subject="s", audience="skchat",
            scopes=["chat.send"], sign=False,
        )
        called = {"n": 0}

        def _boom(*a, **k):  # pragma: no cover - must not run
            called["n"] += 1
            return True

        monkeypatch.setattr("capauth.verify_audience_token", _boom)
        # flag unset (default OFF)
        assert self._validate(_wire(token)) is False
        assert called["n"] == 0


# --------------------------------------------------------------------------- #
# Full end-to-end with a real ephemeral gpg signature (no stubbing)
# --------------------------------------------------------------------------- #
def _gen_gpg_key(gnupghome: Path) -> str | None:
    """Generate an ephemeral gpg key in gnupghome; return its fingerprint or None."""
    if not shutil.which("gpg"):
        return None
    gnupghome.mkdir(parents=True, exist_ok=True)
    gnupghome.chmod(0o700)
    env = {"GNUPGHOME": str(gnupghome)}
    try:
        subprocess.run(
            ["gpg", "--batch", "--yes", "--passphrase", "", "--pinentry-mode",
             "loopback", "--quick-generate-key", "skchat-test@local", "ed25519",
             "sign", "0"],
            capture_output=True, text=True, timeout=30, env={**_os_environ(), **env},
        )
        out = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--list-secret-keys"],
            capture_output=True, text=True, timeout=15, env={**_os_environ(), **env},
        )
        for line in out.stdout.splitlines():
            if line.startswith("fpr:"):
                return line.split(":")[9]
    except (subprocess.SubprocessError, OSError):
        return None
    return None


def _os_environ() -> dict:
    import os

    return dict(os.environ)


class TestAudiencePathRealSignature:
    def test_real_signed_skchat_token_accepted(self, tmp_path, monkeypatch):
        gnupghome = tmp_path / "gnupg"
        fp = _gen_gpg_key(gnupghome)
        if not fp:
            pytest.skip("gpg key generation unavailable in this sandbox")
        monkeypatch.setenv("GNUPGHOME", str(gnupghome))
        home = _agent_home(tmp_path, fingerprint=fp)

        good = mint_audience_token(
            home=home, subject="chef-session", audience="skchat",
            scopes=["chat.send"], sign=True,
        )
        assert good.signature, "token should have been really signed"

        wrong = mint_audience_token(
            home=home, subject="chef-session", audience="skcode",
            scopes=["skcode.stream"], sign=True,
        )
        unscoped = issue_token(
            home=home, subject="chef-session", capabilities=["chat.send"], sign=True,
        )

        monkeypatch.setenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG, "1")
        v = dataplane_auth.CapAuthValidator()
        assert v.validate(_wire(good)) is True          # real signature + audience
        assert v.validate(_wire(wrong)) is False         # wrong audience
        assert v.validate(_wire(unscoped)) is False      # unscoped

        # Same real, valid token is NOT accepted once the flag goes off.
        monkeypatch.delenv(dataplane_auth.ACCEPT_AUDIENCE_ENV_FLAG)
        assert v.validate(_wire(good)) is False
