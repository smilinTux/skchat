from skchat.spaces.federation.trust import AccessLevel, TrustPolicy


def _policy(tmp_path, data):
    import json

    p = tmp_path / "trust.json"
    p.write_text(json.dumps(data))
    return TrustPolicy(path=p)


def test_full_access_host(tmp_path):
    pol = _policy(tmp_path, {"full_access": ["chef.skworld"], "default": "subscribe"})
    assert pol.access_for("lumina@chef.skworld") == AccessLevel.FULL


def test_default_subscribe_for_unknown(tmp_path):
    pol = _policy(tmp_path, {"full_access": ["chef.skworld"], "default": "subscribe"})
    assert pol.access_for("rando@other.realm") == AccessLevel.SUBSCRIBE


def test_default_deny(tmp_path):
    pol = _policy(tmp_path, {"full_access": [], "default": "deny"})
    assert pol.access_for("x@y.z") == AccessLevel.DENY


def test_explicit_fqid_full_access(tmp_path):
    pol = _policy(tmp_path, {"full_access": ["opus@chef.skworld"], "default": "deny"})
    assert pol.access_for("opus@chef.skworld") == AccessLevel.FULL
    assert pol.access_for("other@chef.skworld") == AccessLevel.DENY


def test_missing_config_is_deny_by_default(tmp_path):
    pol = TrustPolicy(path=tmp_path / "nope.json")
    assert pol.access_for("x@y.z") == AccessLevel.DENY


# ── QA Area 3: trust policy edge cases ───────────────────────────────────────


def test_corrupt_json_config_is_deny(tmp_path):
    # A malformed config must not open access — load() swallows the error and the
    # safe defaults (empty full_access, DENY) stand.
    p = tmp_path / "trust.json"
    p.write_text("{ this is not json")
    pol = TrustPolicy(path=p)
    assert pol.access_for("lumina@chef.skworld") == AccessLevel.DENY


def test_invalid_default_value_falls_back_to_deny(tmp_path):
    pol = _policy(tmp_path, {"full_access": [], "default": "wide-open"})
    assert pol.access_for("x@y.z") == AccessLevel.DENY


def test_host_full_access_grants_all_agents_on_that_host(tmp_path):
    pol = _policy(tmp_path, {"full_access": ["chef.skworld"], "default": "deny"})
    assert pol.access_for("anyone@chef.skworld") == AccessLevel.FULL
    assert pol.access_for("else@chef.skworld") == AccessLevel.FULL
    # but a different host is denied
    assert pol.access_for("anyone@evil.attacker") == AccessLevel.DENY


def test_bare_string_without_at_matches_as_host(tmp_path):
    # access_for tolerates a bare host arg (no @) — used when only a host is known
    pol = _policy(tmp_path, {"full_access": ["chef.skworld"], "default": "deny"})
    assert pol.access_for("chef.skworld") == AccessLevel.FULL


def test_explicit_fqid_does_not_grant_sibling_on_same_host(tmp_path):
    # full_access pinned to a SPECIFIC fqid must not leak to a sibling agent
    pol = _policy(tmp_path, {"full_access": ["opus@chef.skworld"], "default": "subscribe"})
    assert pol.access_for("opus@chef.skworld") == AccessLevel.FULL
    assert pol.access_for("lumina@chef.skworld") == AccessLevel.SUBSCRIBE
