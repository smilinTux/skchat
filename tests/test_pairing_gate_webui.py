"""Webui /pair/accept gate enforcement (Funnel hardening)."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import skchat.pairing_gate as pg

    pg._gate = None  # fresh gate per test
    from skcomms import pairing

    monkeypatch.setattr(
        pairing,
        "accept_pairing",
        lambda uri: {"fqid": "lumina@chef.skworld", "fingerprint": "FP"},
    )
    import skchat.webui as webui

    yield TestClient(webui.app)
    pg._gate = None


def test_gate_required_blocks_without_window(client, monkeypatch):
    monkeypatch.setenv("SKCHAT_PAIRING_REQUIRE_GATE", "1")
    r = client.post("/pair/accept", json={"uri": "skp://pair?fqid=x"})
    assert r.status_code == 403
    assert "not open" in r.json()["detail"]


def test_gate_required_allows_with_window_and_nonce(client, monkeypatch):
    monkeypatch.setenv("SKCHAT_PAIRING_REQUIRE_GATE", "1")
    nonce = client.post("/pair/open").json()["nonce"]
    r = client.post("/pair/accept", json={"uri": "skp://x", "nonce": nonce})
    assert r.status_code == 200
    assert r.json()["fqid"] == "lumina@chef.skworld"


def test_gate_required_rejects_wrong_nonce(client, monkeypatch):
    monkeypatch.setenv("SKCHAT_PAIRING_REQUIRE_GATE", "1")
    client.post("/pair/open")
    r = client.post("/pair/accept", json={"uri": "skp://x", "nonce": "wrong"})
    assert r.status_code == 403


def test_tailnet_accept_works_without_gate(client, monkeypatch):
    monkeypatch.delenv("SKCHAT_PAIRING_REQUIRE_GATE", raising=False)
    r = client.post("/pair/accept", json={"uri": "skp://x"})
    assert r.status_code == 200
