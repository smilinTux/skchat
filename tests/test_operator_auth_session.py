import time, pytest
from skchat import operator_auth as oa

def test_mint_then_verify_roundtrip(monkeypatch):
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "test-secret")
    token = oa.mint_operator_session(device_fp="abc123", ttl=60)
    sess = oa.verify_operator_session(token)
    assert sess.device_fp == "abc123"
    assert sess.exp > int(time.time())

def test_expired_is_rejected(monkeypatch):
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "test-secret")
    token = oa.mint_operator_session(device_fp="abc123", ttl=-1)
    with pytest.raises(oa.OperatorAuthError):
        oa.verify_operator_session(token)

def test_wrong_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "secret-a")
    token = oa.mint_operator_session(device_fp="abc123", ttl=60)
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "secret-b")
    with pytest.raises(oa.OperatorAuthError):
        oa.verify_operator_session(token)
