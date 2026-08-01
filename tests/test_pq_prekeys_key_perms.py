"""Root hybrid key at-rest hardening (arch review §3, docs/2026-07-14-arch-review-dm-stack.md).

The root hybrid private key (``~/.skchat/pqc/<agent>_hybrid.key``) was plaintext
0600 with no permission check. These tests cover the loader helpers directly
(pure Python, no liboqs needed) so they run even where ``skcomms``/liboqs is
unavailable:

  * a world/group-readable key file is refused under ``SKCHAT_STRICT_KEY_PERMS``,
  * a 0600 plaintext key still loads (backward compat, flag on or off),
  * an optional sealed/keyring path loads when configured, falling back to
    plaintext when no sealed key is present.
"""

import sys

import pytest

from skchat import pq_prekeys


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(pq_prekeys.STRICT_KEY_PERMS_ENV, raising=False)
    monkeypatch.delenv(pq_prekeys.KEY_KEYRING_ENV, raising=False)


def test_strict_flag_off_by_default_loads_world_readable(tmp_path):
    priv = tmp_path / "agent_hybrid.key"
    priv.write_text("deadbeef")
    priv.chmod(0o644)
    assert pq_prekeys._load_private_key_hex(priv, "agent") == "deadbeef"


def test_strict_flag_refuses_group_readable(tmp_path, monkeypatch):
    monkeypatch.setenv(pq_prekeys.STRICT_KEY_PERMS_ENV, "1")
    priv = tmp_path / "agent_hybrid.key"
    priv.write_text("deadbeef")
    priv.chmod(0o640)
    with pytest.raises(pq_prekeys.InsecureKeyPermissions):
        pq_prekeys._load_private_key_hex(priv, "agent")


def test_strict_flag_refuses_world_readable(tmp_path, monkeypatch):
    monkeypatch.setenv(pq_prekeys.STRICT_KEY_PERMS_ENV, "1")
    priv = tmp_path / "agent_hybrid.key"
    priv.write_text("deadbeef")
    priv.chmod(0o644)
    with pytest.raises(pq_prekeys.InsecureKeyPermissions):
        pq_prekeys._load_private_key_hex(priv, "agent")


def test_strict_flag_still_loads_0600_key(tmp_path, monkeypatch):
    monkeypatch.setenv(pq_prekeys.STRICT_KEY_PERMS_ENV, "1")
    priv = tmp_path / "agent_hybrid.key"
    priv.write_text("deadbeef")
    priv.chmod(0o600)
    assert pq_prekeys._load_private_key_hex(priv, "agent") == "deadbeef"


def test_sealed_keyring_path_used_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv(pq_prekeys.KEY_KEYRING_ENV, "1")

    class _FakeKeyring:
        @staticmethod
        def get_password(service, key):
            assert key == "agent_hybrid"
            return "sealedhex"

    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring)

    priv = tmp_path / "agent_hybrid.key"
    priv.write_text("plaintexthex")
    priv.chmod(0o600)

    assert pq_prekeys._load_private_key_hex(priv, "agent") == "sealedhex"


def test_sealed_keyring_falls_back_to_plaintext_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv(pq_prekeys.KEY_KEYRING_ENV, "1")

    class _FakeKeyring:
        @staticmethod
        def get_password(service, key):
            return None

    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring)

    priv = tmp_path / "agent_hybrid.key"
    priv.write_text("plaintexthex")
    priv.chmod(0o600)

    assert pq_prekeys._load_private_key_hex(priv, "agent") == "plaintexthex"


def test_sealed_keyring_disabled_by_default(tmp_path, monkeypatch):
    """No SKCHAT_KEY_KEYRING set -> never consult keyring, even if importable."""

    class _FakeKeyring:
        @staticmethod
        def get_password(service, key):
            raise AssertionError("keyring should not be consulted when flag is unset")

    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring)

    priv = tmp_path / "agent_hybrid.key"
    priv.write_text("plaintexthex")
    priv.chmod(0o600)

    assert pq_prekeys._load_private_key_hex(priv, "agent") == "plaintexthex"


def test_missing_key_file_returns_none(tmp_path):
    priv = tmp_path / "missing_hybrid.key"
    assert pq_prekeys._load_private_key_hex(priv, "agent") is None


def test_ensure_agent_keypair_propagates_insecure_perms(tmp_path, monkeypatch):
    """End-to-end: an existing keypair with a world-readable key file is
    refused by ensure_agent_keypair under the strict flag, rather than being
    silently regenerated."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv(pq_prekeys.STRICT_KEY_PERMS_ENV, "1")
    monkeypatch.setattr(pq_prekeys, "available", lambda: True)

    d = pq_prekeys._pqc_dir()
    priv_path = d / "agent_hybrid.key"
    pub_path = d / "agent_hybrid.pub"
    priv_path.write_text("deadbeef")
    priv_path.chmod(0o644)
    pub_path.write_text("beefdead")

    with pytest.raises(pq_prekeys.InsecureKeyPermissions):
        pq_prekeys.ensure_agent_keypair("agent")
