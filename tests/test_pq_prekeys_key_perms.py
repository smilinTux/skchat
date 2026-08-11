"""PQC root hybrid key at-rest hardening (arch review section 3).

The root hybrid private key (``~/.skchat/pqc/<agent>_hybrid.key``) is plaintext
hex protected only by ``0600``. This proves:

  * under ``SKCHAT_STRICT_KEY_PERMS``, a group/world-readable key file is
    refused rather than silently loaded,
  * the historical plaintext-0600 file keeps loading unchanged (strict flag
    unset, or strict flag set with correct perms),
  * an optional sealed/keyring backend (``SKCHAT_KEY_BACKEND=keyring``) is
    preferred over the plaintext file when a sealed entry is present, and
    plaintext is still the fallback when it isn't.

``ensure_agent_keypair`` gates on ``available()`` (liboqs via skcomms.pqkem),
so every test monkeypatches that to True and drives the load path directly
with hand-written tmp key files - no real PQ backend required.
"""

import os

import pytest


@pytest.fixture()
def pq_home(tmp_path, monkeypatch):
    """Fresh pq_prekeys bound to an isolated SKCHAT_HOME, PQ backend forced on."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    import importlib

    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    monkeypatch.setattr(pq_prekeys, "available", lambda: True)
    return pq_prekeys


def _write_plaintext_key(pq_home, agent: str, *, pub_hex: str, priv_hex: str, mode: int):
    d = pq_home._pqc_dir()
    pub_path = d / f"{agent}_hybrid.pub"
    priv_path = d / f"{agent}_hybrid.key"
    pub_path.write_text(pub_hex)
    priv_path.write_text(priv_hex)
    os.chmod(priv_path, mode)
    return pub_path, priv_path


def test_0600_plaintext_key_still_loads(pq_home, monkeypatch):
    """Backward compat: strict flag unset, 0600 file loads exactly as before."""
    monkeypatch.delenv("SKCHAT_STRICT_KEY_PERMS", raising=False)
    _write_plaintext_key(pq_home, "lumina", pub_hex="aa" * 8, priv_hex="bb" * 8, mode=0o600)

    pub, priv = pq_home.ensure_agent_keypair("lumina")
    assert pub == bytes.fromhex("aa" * 8)
    assert priv == bytes.fromhex("bb" * 8)


def test_0600_plaintext_key_still_loads_under_strict_flag(pq_home, monkeypatch):
    """Strict flag set, but perms are already correct - no refusal."""
    monkeypatch.setenv("SKCHAT_STRICT_KEY_PERMS", "1")
    _write_plaintext_key(pq_home, "lumina", pub_hex="aa" * 8, priv_hex="bb" * 8, mode=0o600)

    pub, priv = pq_home.ensure_agent_keypair("lumina")
    assert pub == bytes.fromhex("aa" * 8)
    assert priv == bytes.fromhex("bb" * 8)


def test_group_readable_key_refused_under_strict_flag(pq_home, monkeypatch):
    monkeypatch.setenv("SKCHAT_STRICT_KEY_PERMS", "1")
    _write_plaintext_key(pq_home, "lumina", pub_hex="aa" * 8, priv_hex="bb" * 8, mode=0o640)

    with pytest.raises(pq_home.InsecureKeyPermissionsError):
        pq_home.ensure_agent_keypair("lumina")


def test_world_readable_key_refused_under_strict_flag(pq_home, monkeypatch):
    monkeypatch.setenv("SKCHAT_STRICT_KEY_PERMS", "1")
    _write_plaintext_key(pq_home, "lumina", pub_hex="aa" * 8, priv_hex="bb" * 8, mode=0o644)

    with pytest.raises(pq_home.InsecureKeyPermissionsError):
        pq_home.ensure_agent_keypair("lumina")


def test_world_readable_key_loads_when_strict_flag_unset(pq_home, monkeypatch):
    """The strict check is opt-in: unset, a loose-permission file still loads."""
    monkeypatch.delenv("SKCHAT_STRICT_KEY_PERMS", raising=False)
    _write_plaintext_key(pq_home, "lumina", pub_hex="aa" * 8, priv_hex="bb" * 8, mode=0o644)

    pub, priv = pq_home.ensure_agent_keypair("lumina")
    assert pub == bytes.fromhex("aa" * 8)
    assert priv == bytes.fromhex("bb" * 8)


def test_sealed_keyring_backend_loads_when_configured(pq_home, monkeypatch):
    """SKCHAT_KEY_BACKEND=keyring: a sealed entry is preferred over plaintext."""
    monkeypatch.setenv("SKCHAT_KEY_BACKEND", "keyring")
    d = pq_home._pqc_dir()
    (d / "lumina_hybrid.pub").write_text("aa" * 8)
    # No plaintext .key file at all - the sealed entry is the only source.

    calls = {}

    class FakeKeyring:
        @staticmethod
        def get_password(service, username):
            calls["args"] = (service, username)
            return "cc" * 8

    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)

    pub, priv = pq_home.ensure_agent_keypair("lumina")
    assert pub == bytes.fromhex("aa" * 8)
    assert priv == bytes.fromhex("cc" * 8)
    assert calls["args"] == (pq_home._KEYRING_SERVICE, "lumina_hybrid")


def test_sealed_backend_falls_back_to_plaintext_when_no_sealed_entry(pq_home, monkeypatch):
    """SKCHAT_KEY_BACKEND=keyring but nothing sealed yet - plaintext still loads."""
    monkeypatch.setenv("SKCHAT_KEY_BACKEND", "keyring")
    _write_plaintext_key(pq_home, "lumina", pub_hex="aa" * 8, priv_hex="bb" * 8, mode=0o600)

    class FakeKeyring:
        @staticmethod
        def get_password(service, username):
            return None

    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)

    pub, priv = pq_home.ensure_agent_keypair("lumina")
    assert pub == bytes.fromhex("aa" * 8)
    assert priv == bytes.fromhex("bb" * 8)


def test_sealed_backend_falls_back_to_plaintext_when_keyring_package_missing(
    pq_home, monkeypatch
):
    """SKCHAT_KEY_BACKEND=keyring but the optional `keyring` package isn't installed."""
    import builtins
    import sys

    monkeypatch.setenv("SKCHAT_KEY_BACKEND", "keyring")
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "keyring":
            raise ImportError("no module named keyring")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    _write_plaintext_key(pq_home, "lumina", pub_hex="aa" * 8, priv_hex="bb" * 8, mode=0o600)

    pub, priv = pq_home.ensure_agent_keypair("lumina")
    assert pub == bytes.fromhex("aa" * 8)
    assert priv == bytes.fromhex("bb" * 8)


def test_default_backend_ignores_keyring_even_if_present(pq_home, monkeypatch):
    """SKCHAT_KEY_BACKEND unset: sealed lookup is skipped entirely, plaintext wins."""
    monkeypatch.delenv("SKCHAT_KEY_BACKEND", raising=False)
    _write_plaintext_key(pq_home, "lumina", pub_hex="aa" * 8, priv_hex="bb" * 8, mode=0o600)

    class FakeKeyring:
        @staticmethod
        def get_password(service, username):
            raise AssertionError("keyring should not be consulted when backend is unset")

    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)

    pub, priv = pq_home.ensure_agent_keypair("lumina")
    assert pub == bytes.fromhex("aa" * 8)
    assert priv == bytes.fromhex("bb" * 8)
