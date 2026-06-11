import base64
import hashlib
import hmac

from skchat.connectivity import ice_config


def test_tier1_both_on_tailnet_has_no_relay(monkeypatch):
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    cfg = ice_config(
        local_fqid="lumina@chef.skworld",
        peer_fqid="opus@chef.skworld",
        peer_hint={"on_tailnet": True},
    )
    assert cfg["preferred_tier"] == 1
    assert cfg["on_tailnet"] is True
    assert cfg["ice_servers"] == []


def test_tier3_cross_nat_emits_ephemeral_turn(monkeypatch):
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "s3cr3t")
    monkeypatch.setenv(
        "SKCHAT_TURN_URLS", "turn:turn.example.com:3478?transport=udp"
    )
    cfg = ice_config(
        local_fqid="lumina@chef.skworld",
        peer_fqid="opus@chef.skworld",
        peer_hint={"on_tailnet": False},
    )
    assert cfg["preferred_tier"] == 3
    turn = [s for s in cfg["ice_servers"] if any("turn:" in u for u in s["urls"])]
    assert turn, "expected a TURN server entry"
    entry = turn[0]
    assert ":" in entry["username"]
    expiry, _, who = entry["username"].partition(":")
    assert who == "lumina@chef.skworld"
    expected = base64.b64encode(
        hmac.new(b"s3cr3t", entry["username"].encode(), hashlib.sha1).digest()
    ).decode()
    assert entry["credential"] == expected


def test_secret_never_appears_in_config(monkeypatch):
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "topsecret-xyz")
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turn:turn.example.com:3478")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    assert "topsecret-xyz" not in repr(cfg)
