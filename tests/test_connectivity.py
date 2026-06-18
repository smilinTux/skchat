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
    # Disable the free public TURN fallback so this stays a pure STUN-only case.
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_ENABLED", "false")
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


def test_no_servers_when_everything_disabled(monkeypatch):
    # Explicitly empty STUN + no sovereign TURN + public TURN off → zero servers.
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.delenv("SKCHAT_TURN_URLS", raising=False)
    monkeypatch.setenv("SKCHAT_STUN_URLS", "")
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_ENABLED", "false")
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


# ── Free public ICE defaults (Sovereign Conf Calls — d5b00d43) ───────────────
# Precedence: sovereign coturn > free public TURN > STUN-only > tailnet-direct.


def _clear_public_env(monkeypatch):
    for v in (
        "SKCHAT_TURN_SECRET",
        "SKCHAT_TURN_URLS",
        "SKCHAT_STUN_URLS",
        "SKCHAT_PUBLIC_TURN_ENABLED",
        "SKCHAT_PUBLIC_TURN_URLS",
        "SKCHAT_PUBLIC_TURN_USER",
        "SKCHAT_PUBLIC_TURN_CRED",
    ):
        monkeypatch.delenv(v, raising=False)


def test_public_defaults_include_google_stun_and_open_relay_turn(monkeypatch):
    # With NO TURN/STUN env set, public conf calls still work: Google STUN +
    # Open Relay free public TURN are emitted by default.
    _clear_public_env(monkeypatch)
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    assert cfg["preferred_tier"] == 3
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    # Google's free STUN.
    assert "stun:stun.l.google.com:19302" in flat
    assert sum(u.startswith("stun:stun") and "google" in u for u in flat) >= 3
    # Open Relay free public TURN (multiple ports/transports) with its creds.
    turn = next(s for s in cfg["ice_servers"] if any("turn:" in u for u in s["urls"]))
    assert "turn:openrelay.metered.ca:80" in turn["urls"]
    assert "turn:openrelay.metered.ca:443" in turn["urls"]
    assert "turn:openrelay.metered.ca:443?transport=tcp" in turn["urls"]
    assert turn["username"] == "openrelayproject"
    assert turn["credential"] == "openrelayproject"


def test_sovereign_coturn_preferred_over_free_public_turn(monkeypatch):
    # When SKCHAT_TURN_SECRET is set, the sovereign coturn wins — the free public
    # Open Relay TURN must NOT appear (sovereign overrides free).
    _clear_public_env(monkeypatch)
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "s3cr3t")
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turn:turn.sovereign.example:3478")
    cfg = ice_config("lumina@chef.skworld", "b@x.y", peer_hint={"on_tailnet": False})
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    # Sovereign TURN present, ephemeral-credentialed.
    turn = next(s for s in cfg["ice_servers"] if any("turn:" in u for u in s["urls"]))
    assert "turn:turn.sovereign.example:3478" in turn["urls"]
    assert ":" in turn["username"] and turn["username"].endswith("lumina@chef.skworld")
    # No free public Open Relay TURN.
    assert not any("openrelay.metered.ca" in u for u in flat)


def test_public_turn_disabled_emits_no_free_turn(monkeypatch):
    # SKCHAT_PUBLIC_TURN_ENABLED=false → STUN stays, but no free TURN at all.
    _clear_public_env(monkeypatch)
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_ENABLED", "false")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    assert any(u.startswith("stun:") for u in flat)  # STUN still offered
    assert not any("turn:" in u for u in flat)  # no TURN
    assert all("username" not in s for s in cfg["ice_servers"])


def test_free_public_turn_is_env_overridable(monkeypatch):
    # Operators can repoint the free TURN at any provider + creds.
    _clear_public_env(monkeypatch)
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_URLS", "turn:relay.other.example:3478")
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_USER", "myuser")
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_CRED", "mycred")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    turn = next(s for s in cfg["ice_servers"] if any("turn:" in u for u in s["urls"]))
    assert turn["urls"] == ["turn:relay.other.example:3478"]
    assert turn["username"] == "myuser"
    assert turn["credential"] == "mycred"
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    assert not any("openrelay.metered.ca" in u for u in flat)


def test_tailnet_never_emits_public_servers(monkeypatch):
    # Tier-1 tailnet stays first: no STUN/TURN even with public defaults active.
    _clear_public_env(monkeypatch)
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": True})
    assert cfg["preferred_tier"] == 1
    assert cfg["ice_servers"] == []


def test_explicit_stun_override_replaces_google_default(monkeypatch):
    _clear_public_env(monkeypatch)
    monkeypatch.setenv("SKCHAT_STUN_URLS", "stun:my.stun.example:3478")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    assert "stun:my.stun.example:3478" in flat
    assert not any("google" in u for u in flat)
