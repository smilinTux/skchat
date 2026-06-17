import base64
import hashlib
import hmac
import time

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
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turn:turn.example.com:3478?transport=udp")
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
        hmac.HMAC(b"s3cr3t", entry["username"].encode(), hashlib.sha1).digest()
    ).decode()
    assert entry["credential"] == expected


def test_tier2_same_subnet_has_no_relay(monkeypatch):
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    cfg = ice_config(
        local_fqid="lumina@chef.skworld",
        peer_fqid="opus@chef.skworld",
        peer_hint={"same_subnet": True},
    )
    assert cfg["preferred_tier"] == 2
    assert cfg["on_tailnet"] is False
    assert cfg["ice_servers"] == []


def test_tier3_stun_only_when_no_turn_secret(monkeypatch):
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.setenv("SKCHAT_STUN_URLS", "stun:turn.example.com:3478")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    assert cfg["preferred_tier"] == 3
    # a STUN entry is present; no TURN entry (no secret)
    assert any("stun:" in u for s in cfg["ice_servers"] for u in s["urls"])
    assert all("username" not in s for s in cfg["ice_servers"])


def test_secret_never_appears_in_config(monkeypatch):
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "topsecret-xyz")
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turn:turn.example.com:3478")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    assert "topsecret-xyz" not in repr(cfg)


# ── QA Area 3: connectivity edge cases ───────────────────────────────────────


def test_tailnet_takes_precedence_over_subnet(monkeypatch):
    # If both hints are true, tier 1 (tailnet) wins — the cheapest path first.
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": True, "same_subnet": True})
    assert cfg["preferred_tier"] == 1


def test_no_hint_defaults_to_tier3(monkeypatch):
    # With no reachability hint at all, fall through to the relay tier.
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.delenv("SKCHAT_STUN_URLS", raising=False)
    cfg = ice_config("a@x.y", "b@x.y", peer_hint=None)
    assert cfg["preferred_tier"] == 3
    assert cfg["on_tailnet"] is False


def test_turn_credential_expiry_is_in_the_future(monkeypatch):
    # The ephemeral coturn username encodes a future unix expiry (ttl ahead).
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "s")
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turn:turn.example.com:3478")
    cfg = ice_config("me@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    turn = next(s for s in cfg["ice_servers"] if any("turn:" in u for u in s["urls"]))
    expiry = int(turn["username"].split(":", 1)[0])
    assert expiry > int(time.time())


def test_stun_and_turn_both_emitted_when_configured(monkeypatch):
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "s")
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turn:relay:3478")
    monkeypatch.setenv("SKCHAT_STUN_URLS", "stun:relay:3478")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    assert any("stun:" in u for u in flat)
    assert any("turn:" in u for u in flat)


def test_no_servers_when_no_turn_or_stun_config(monkeypatch):
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.delenv("SKCHAT_TURN_URLS", raising=False)
    monkeypatch.delenv("SKCHAT_STUN_URLS", raising=False)
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    assert cfg["ice_servers"] == []
    assert cfg["preferred_tier"] == 3  # still relay tier, just no servers to offer


def test_distinct_identities_get_distinct_credentials(monkeypatch):
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "s")
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turn:relay:3478")
    c1 = ice_config("alice@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    c2 = ice_config("bob@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    u1 = next(s for s in c1["ice_servers"] if "username" in s)["username"]
    u2 = next(s for s in c2["ice_servers"] if "username" in s)["username"]
    assert "alice@x.y" in u1 and "bob@x.y" in u2 and u1 != u2
