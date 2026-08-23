from capauth.pairing import operator_session as oa

from skchat.dataplane_auth import CapAuthValidator


def test_validator_accepts_operator_session(monkeypatch):
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "sec")
    oa.approve_device("abc123")
    token = oa.mint_operator_session(device_fp="abc123", ttl=60)
    assert CapAuthValidator().validate(token) is True


def test_validator_rejects_garbage(monkeypatch):
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "sec")
    assert CapAuthValidator().validate("not-a-token") is False
