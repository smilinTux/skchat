"""Revocation by device_fp: one write kills every session a device holds."""

from __future__ import annotations

import pytest

from skchat import guest as G
from skchat import operator_auth as OA


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    yield


def test_a_revoked_device_fp_is_reported_revoked_and_survives_a_cache_drop():
    assert G.is_device_revoked("aa" * 8) is False
    G.revoke_device("aa" * 8)
    assert G.is_device_revoked("aa" * 8) is True
    # Simulate a process restart: the SQLite row, not the cache, is the truth.
    G._reset_device_revocation_cache()
    assert G.is_device_revoked("aa" * 8) is True


def test_every_session_of_a_revoked_device_dies_at_once():
    fp = "bb" * 8
    first = OA.mint_operator_session(device_fp=fp)
    second = OA.mint_operator_session(device_fp=fp)
    assert OA.verify_operator_session(first).device_fp == fp
    assert OA.verify_operator_session(second).device_fp == fp

    G.revoke_device(fp)

    # BOTH sessions die from the single revocation, without either jti being known.
    for token in (first, second):
        with pytest.raises(OA.OperatorAuthError):
            OA.verify_operator_session(token)


def test_revoking_one_device_leaves_another_devices_session_working():
    keep = OA.mint_operator_session(device_fp="cc" * 8)
    G.revoke_device("dd" * 8)
    assert OA.verify_operator_session(keep).device_fp == "cc" * 8


def test_unrevoke_lets_a_relinked_device_authenticate_again():
    fp = "ee" * 8
    G.revoke_device(fp)
    token_after_relink = OA.mint_operator_session(device_fp=fp)
    with pytest.raises(OA.OperatorAuthError):
        OA.verify_operator_session(token_after_relink)
    G.unrevoke_device(fp)
    assert OA.verify_operator_session(token_after_relink).device_fp == fp


def test_revoke_device_is_idempotent():
    G.revoke_device("ff" * 8)
    G.revoke_device("ff" * 8)
    assert G.is_device_revoked("ff" * 8) is True
