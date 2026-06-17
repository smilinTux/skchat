"""Unit tests for skchat.adapter_bind — CapAuth /bind flow + binding store.

CapAuth and the skcomms adapter are injected as mocks/fakes: no PGP keys, no
live bots, no network.  The real skcomms ``PlatformIdentity`` /
``FakeAdapter`` are used so the bind path is exercised against the actual
adapter shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from skcomms.adapters.fake import FakeAdapter
from skcomms.adapters.models import ChannelType, PlatformIdentity

from skchat.adapter_bind import (
    TRUST_VERIFIED,
    AdapterBinder,
    BindResult,
    FqidBindingStore,
    parse_bind_command,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _platform(platform_id: str = "123456789") -> PlatformIdentity:
    return PlatformIdentity(
        channel=ChannelType.TELEGRAM,
        platform_id=platform_id,
        platform_name="Chef David",
        room_id="-5134021983",
    )


@pytest.fixture
def adapter():
    return FakeAdapter({"adapter_name": "telegram"})


@pytest.fixture
def ok_verifier():
    v = MagicMock()
    v.verify = AsyncMock(return_value=True)
    return v


@pytest.fixture
def fail_verifier():
    v = MagicMock()
    v.verify = AsyncMock(return_value=False)
    return v


# ---------------------------------------------------------------------------
# parse_bind_command
# ---------------------------------------------------------------------------


class TestParseBindCommand:
    def test_valid_command(self):
        is_cmd, fqid, reason = parse_bind_command("/bind chef@skworld.io")
        assert is_cmd is True
        assert fqid == "chef@skworld.io"
        assert reason is None

    def test_capauth_uri_fqid(self):
        _is_cmd, fqid, _reason = parse_bind_command("/bind capauth:lumina@skworld.io")
        assert fqid == "capauth:lumina@skworld.io"

    def test_case_insensitive_verb(self):
        is_cmd, fqid, _reason = parse_bind_command("/BIND chef@skworld.io")
        assert is_cmd is True
        assert fqid == "chef@skworld.io"

    def test_extra_whitespace_tolerated(self):
        _is_cmd, fqid, _reason = parse_bind_command("   /bind   chef@skworld.io   ")
        assert fqid == "chef@skworld.io"

    def test_not_a_command(self):
        is_cmd, fqid, reason = parse_bind_command("hello there")
        assert is_cmd is False
        assert fqid is None
        assert reason == "not_a_command"

    def test_missing_fqid(self):
        is_cmd, fqid, reason = parse_bind_command("/bind")
        assert is_cmd is True
        assert fqid is None
        assert reason == "missing_fqid"

    def test_bad_fqid(self):
        is_cmd, fqid, reason = parse_bind_command("/bind not-an-fqid")
        assert is_cmd is True
        assert fqid is None
        assert reason == "bad_fqid"

    def test_empty_string(self):
        is_cmd, _fqid, _reason = parse_bind_command("")
        assert is_cmd is False


# ---------------------------------------------------------------------------
# FqidBindingStore
# ---------------------------------------------------------------------------


class TestFqidBindingStore:
    def test_in_memory_put_get(self):
        store = FqidBindingStore(path=None)
        store.put("telegram:user:1", "chef@skworld.io")
        assert store.get("telegram:user:1") == "chef@skworld.io"

    def test_missing_key_returns_none(self):
        store = FqidBindingStore(path=None)
        assert store.get("telegram:user:404") is None

    def test_persists_to_disk(self, tmp_path):
        path = tmp_path / "bindings.yml"
        store = FqidBindingStore(path=path)
        store.put("telegram:user:1", "chef@skworld.io")
        assert path.exists()

    def test_survives_restart(self, tmp_path):
        path = tmp_path / "bindings.yml"
        FqidBindingStore(path=path).put("telegram:user:1", "chef@skworld.io")
        # A *new* store instance reads the persisted file.
        reloaded = FqidBindingStore(path=path)
        assert reloaded.get("telegram:user:1") == "chef@skworld.io"

    def test_overwrite_existing(self, tmp_path):
        path = tmp_path / "bindings.yml"
        store = FqidBindingStore(path=path)
        store.put("telegram:user:1", "old@skworld.io")
        store.put("telegram:user:1", "new@skworld.io")
        assert FqidBindingStore(path=path).get("telegram:user:1") == "new@skworld.io"

    def test_all_returns_copy(self):
        store = FqidBindingStore(path=None)
        store.put("k", "v")
        snapshot = store.all()
        snapshot["k"] = "mutated"
        assert store.get("k") == "v"

    def test_corrupt_file_does_not_crash(self, tmp_path):
        path = tmp_path / "bindings.yml"
        path.write_text("{ this is not: valid: yaml ::::", encoding="utf-8")
        store = FqidBindingStore(path=path)  # must not raise
        assert store.get("anything") is None

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "bindings.yml"
        FqidBindingStore(path=path).put("k", "v")
        assert path.exists()


# ---------------------------------------------------------------------------
# AdapterBinder — full /bind flow
# ---------------------------------------------------------------------------


class TestAdapterBinder:
    @pytest.mark.asyncio
    async def test_successful_bind(self, adapter, ok_verifier):
        binder = AdapterBinder(adapter, verifier=ok_verifier)
        plat = _platform()
        result = await binder.bind(plat, "/bind chef@skworld.io")
        assert isinstance(result, BindResult)
        assert result.ok is True
        assert result.fqid == "chef@skworld.io"
        # The adapter now resolves the platform id to the bound FQID.
        assert await adapter.resolve_fqid(plat) == "chef@skworld.io"

    @pytest.mark.asyncio
    async def test_verifier_called_with_fqid_and_platform(self, adapter, ok_verifier):
        binder = AdapterBinder(adapter, verifier=ok_verifier)
        plat = _platform()
        await binder.bind(plat, "/bind chef@skworld.io")
        ok_verifier.verify.assert_awaited_once_with("chef@skworld.io", plat)

    @pytest.mark.asyncio
    async def test_adapter_bind_fqid_invoked_with_verified_trust(self, ok_verifier):
        adapter = MagicMock()
        adapter.bind_fqid = AsyncMock()
        binder = AdapterBinder(adapter, verifier=ok_verifier)
        plat = _platform()
        await binder.bind(plat, "/bind chef@skworld.io")
        adapter.bind_fqid.assert_awaited_once_with(
            plat, "chef@skworld.io", TRUST_VERIFIED
        )

    @pytest.mark.asyncio
    async def test_challenge_failure_does_not_bind(self, adapter, fail_verifier):
        binder = AdapterBinder(adapter, verifier=fail_verifier)
        plat = _platform()
        result = await binder.bind(plat, "/bind chef@skworld.io")
        assert result.ok is False
        assert result.reason == "challenge_failed"
        # No binding written.
        assert await adapter.resolve_fqid(plat) is None

    @pytest.mark.asyncio
    async def test_no_verifier_refuses_bind(self, adapter):
        binder = AdapterBinder(adapter, verifier=None)
        result = await binder.bind(_platform(), "/bind chef@skworld.io")
        assert result.ok is False
        assert result.reason == "verifier_error"

    @pytest.mark.asyncio
    async def test_verifier_exception_is_safe(self, adapter):
        v = MagicMock()
        v.verify = AsyncMock(side_effect=RuntimeError("capauth down"))
        binder = AdapterBinder(adapter, verifier=v)
        result = await binder.bind(_platform(), "/bind chef@skworld.io")
        assert result.ok is False
        assert result.reason == "verifier_error"
        assert await adapter.resolve_fqid(_platform()) is None

    @pytest.mark.asyncio
    async def test_non_command_short_circuits(self, adapter, ok_verifier):
        binder = AdapterBinder(adapter, verifier=ok_verifier)
        result = await binder.bind(_platform(), "just a normal message")
        assert result.ok is False
        assert result.reason == "not_a_command"
        ok_verifier.verify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_fqid_fails_before_challenge(self, adapter, ok_verifier):
        binder = AdapterBinder(adapter, verifier=ok_verifier)
        result = await binder.bind(_platform(), "/bind")
        assert result.ok is False
        assert result.reason == "missing_fqid"
        ok_verifier.verify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bad_fqid_fails_before_challenge(self, adapter, ok_verifier):
        binder = AdapterBinder(adapter, verifier=ok_verifier)
        result = await binder.bind(_platform(), "/bind garbage")
        assert result.ok is False
        assert result.reason == "bad_fqid"
        ok_verifier.verify.assert_not_awaited()

    # -- persistence (restart durability) -----------------------------------

    @pytest.mark.asyncio
    async def test_bind_mirrored_to_store(self, adapter, ok_verifier, tmp_path):
        path = tmp_path / "bindings.yml"
        store = FqidBindingStore(path=path)
        binder = AdapterBinder(adapter, verifier=ok_verifier, store=store)
        plat = _platform()
        await binder.bind(plat, "/bind chef@skworld.io")
        assert store.get(plat.canonical_key) == "chef@skworld.io"

    @pytest.mark.asyncio
    async def test_binding_survives_restart(self, adapter, ok_verifier, tmp_path):
        path = tmp_path / "bindings.yml"
        binder = AdapterBinder(
            adapter, verifier=ok_verifier, store=FqidBindingStore(path=path)
        )
        plat = _platform()
        await binder.bind(plat, "/bind chef@skworld.io")
        # Fresh store from disk → binding is still there.
        reloaded = FqidBindingStore(path=path)
        assert reloaded.get(plat.canonical_key) == "chef@skworld.io"

    @pytest.mark.asyncio
    async def test_failed_bind_not_persisted(self, adapter, fail_verifier, tmp_path):
        path = tmp_path / "bindings.yml"
        store = FqidBindingStore(path=path)
        binder = AdapterBinder(adapter, verifier=fail_verifier, store=store)
        plat = _platform()
        await binder.bind(plat, "/bind chef@skworld.io")
        assert store.get(plat.canonical_key) is None
