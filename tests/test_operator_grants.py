"""The enrolled operator device must be granted BOTH skchat.prekey and the
least-sensitive skchat.inbox capability, non-expiring, so the authz PDP agrees
with the legitimate legacy allow for the operator seat's own inbox polling
(CR-3.1: closes the 227k-hit shadow divergence that blocked the enforce flip).
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    # capauth default_base_dir() == Path.home()/.skcapstone; pin home to tmp.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    base = tmp_path / ".skcapstone"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _caps_for(home_dir, subject):
    from capauth.tokens import list_tokens

    caps = set()
    for t in list_tokens(home_dir):
        subj = getattr(t.payload, "subject", None)
        if subj == subject and t.payload.is_active:
            caps.update(getattr(t.payload, "capabilities", []) or [])
    return caps


def test_grant_mints_prekey_and_inbox_nonexpiring(home):
    from skchat.dataplane_auth import operator_subject
    from skchat.operator_grants import grant_operator_capabilities

    device_fp = "abc123deadbeef01"
    # a throwaway ed25519-ish pubkey (base64); the grant records the device + tokens
    pubkey_b64 = "TESTPUBKEYb64AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

    ok = grant_operator_capabilities(device_fp, pubkey_b64)
    assert ok is True

    subject = operator_subject(device_fp)
    caps = _caps_for(home, subject)
    assert "skchat.prekey" in caps, caps
    assert "skchat.inbox" in caps, caps

    # non-expiring: no active token for the subject carries an expiry
    from capauth.tokens import list_tokens

    for t in list_tokens(home):
        if getattr(t.payload, "subject", None) == subject:
            assert t.payload.expires_at is None, "operator grant must not expire"


def test_backfill_adds_inbox_to_existing_prekey_only_device(home):
    """A device previously granted prekey-only gets inbox added by the backfill."""
    from capauth.tokens import issue_token

    from skchat.dataplane_auth import operator_subject
    from skchat.operator_grants import backfill_operator_capabilities

    subject = operator_subject("f00dfeed12345678")
    # simulate the old-world state: a prekey-only grant
    issue_token(home, subject, ["skchat.prekey"], sign=False)
    assert "skchat.inbox" not in _caps_for(home, subject)

    n = backfill_operator_capabilities(base_dir=home)
    assert n >= 1
    assert "skchat.inbox" in _caps_for(home, subject)
