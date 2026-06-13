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
