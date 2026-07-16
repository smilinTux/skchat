import base64
import hashlib
import hmac
import time

from skchat.connectivity import ice_config

# Any hostname/marker that would indicate a non-sovereign, third-party relay.
# The ICE ladder must NEVER emit one of these - sovereign-only, fail closed.
_NON_SOVEREIGN_MARKERS = ("openrelay", "metered.ca", "twilio", "xirsys")


def _assert_no_non_sovereign_turn(cfg: dict) -> None:
    flat = [u.lower() for s in cfg["ice_servers"] for u in s["urls"]]
    for marker in _NON_SOVEREIGN_MARKERS:
        assert not any(marker in u for u in flat), f"non-sovereign relay marker {marker!r} found in {flat}"
    for s in cfg["ice_servers"]:
        if "username" in s or "credential" in s:
            # Any credentialed entry must be TURN, and must not be the known
            # openrelay static creds.
            assert s.get("username") != "openrelayproject"
            assert s.get("credential") != "openrelayproject"


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
    # a STUN entry is present; no TURN entry (no secret) - fail closed, no relay.
    assert any("stun:" in u for s in cfg["ice_servers"] for u in s["urls"])
    assert all("username" not in s for s in cfg["ice_servers"])
    _assert_no_non_sovereign_turn(cfg)


def test_secret_never_appears_in_config(monkeypatch):
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "topsecret-xyz")
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turn:turn.example.com:3478")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    assert "topsecret-xyz" not in repr(cfg)


# ── QA Area 3: connectivity edge cases ───────────────────────────────────────


def test_tailnet_takes_precedence_over_subnet(monkeypatch):
    # If both hints are true, tier 1 (tailnet) wins - the cheapest path first.
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
    # Explicitly empty STUN + no sovereign TURN → zero servers, fail closed.
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.delenv("SKCHAT_TURN_URLS", raising=False)
    monkeypatch.setenv("SKCHAT_STUN_URLS", "")
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


# ── Sovereign-only TURN, fail closed (skchat Resilience v1, coord 10386e96) ──
# Precedence: sovereign coturn > STUN-only > (nothing - no third-party relay,
# ever). There is no opt-in fallback tier: with no sovereign coturn configured,
# the relay tier fails closed (STUN candidates only, or none).


def _clear_sovereign_env(monkeypatch):
    for v in (
        "SKCHAT_TURN_SECRET",
        "SKCHAT_TURN_URLS",
        "SKCHAT_STUN_URLS",
    ):
        monkeypatch.delenv(v, raising=False)


def test_defaults_offer_google_stun_and_never_a_third_party_turn(monkeypatch):
    # With NO TURN/STUN env set, cross-NAT peers get Google STUN only. There is
    # no third-party TURN fallback tier at all - fail closed to STUN-only.
    _clear_sovereign_env(monkeypatch)
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    assert cfg["preferred_tier"] == 3
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    # Google's free STUN is still offered (STUN only, never a relay).
    assert "stun:stun.l.google.com:19302" in flat
    assert sum(u.startswith("stun:stun") and "google" in u for u in flat) >= 3
    assert not any("turn:" in u for u in flat)
    assert all("username" not in s for s in cfg["ice_servers"])
    _assert_no_non_sovereign_turn(cfg)


def test_sovereign_only_when_configured(monkeypatch):
    # When SKCHAT_TURN_SECRET + SKCHAT_TURN_URLS are set, the sovereign coturn is
    # the ONLY relay ever emitted.
    _clear_sovereign_env(monkeypatch)
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "s3cr3t")
    monkeypatch.setenv(
        "SKCHAT_TURN_URLS",
        "turn:noroc2027.tail204f0c.ts.net:443?transport=tls,"
        "turn:noroc2027.tail204f0c.ts.net:3478?transport=udp",
    )
    cfg = ice_config("lumina@chef.skworld", "b@x.y", peer_hint={"on_tailnet": False})
    # Sovereign TURN present, ephemeral-credentialed, both TLS + udp forms.
    turn = next(s for s in cfg["ice_servers"] if any("turn:" in u for u in s["urls"]))
    assert "turn:noroc2027.tail204f0c.ts.net:443?transport=tls" in turn["urls"]
    assert "turn:noroc2027.tail204f0c.ts.net:3478?transport=udp" in turn["urls"]
    assert ":" in turn["username"] and turn["username"].endswith("lumina@chef.skworld")
    # Exactly one relay entry - sovereign only, nothing appended alongside it.
    relay_entries = [s for s in cfg["ice_servers"] if s.get("credential")]
    assert len(relay_entries) == 1
    _assert_no_non_sovereign_turn(cfg)


def test_fails_closed_when_sovereign_coturn_not_configured(monkeypatch):
    # No sovereign coturn configured -> no relay of any kind is emitted, ever.
    # This is the fail-closed invariant: calling never reaches a third-party
    # relay just because the sovereign one is unavailable.
    _clear_sovereign_env(monkeypatch)
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    assert not any("turn:" in u for u in flat)
    _assert_no_non_sovereign_turn(cfg)


def test_no_opt_in_env_can_summon_a_third_party_relay(monkeypatch):
    # There is no environment variable left that can turn on a third-party
    # relay - the old opt-in gates are gone, not just defaulted off. Setting
    # them (if a stale deploy env still has them) must have zero effect.
    _clear_sovereign_env(monkeypatch)
    monkeypatch.setenv("SKCHAT_ALLOW_OPENRELAY", "true")
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_ENABLED", "true")
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_URLS", "turn:openrelay.metered.ca:80")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    assert not any("turn:" in u for u in flat)
    _assert_no_non_sovereign_turn(cfg)


def test_tailnet_never_emits_public_servers(monkeypatch):
    # Tier-1 tailnet stays first: no STUN/TURN even off-tailnet defaults exist.
    _clear_sovereign_env(monkeypatch)
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": True})
    assert cfg["preferred_tier"] == 1
    assert cfg["ice_servers"] == []


def test_explicit_stun_override_replaces_google_default(monkeypatch):
    _clear_sovereign_env(monkeypatch)
    monkeypatch.setenv("SKCHAT_STUN_URLS", "stun:my.stun.example:3478")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    flat = [u for s in cfg["ice_servers"] for u in s["urls"]]
    assert "stun:my.stun.example:3478" in flat
    assert not any("google" in u for u in flat)
