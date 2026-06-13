import json

from skchat.spaces.federation.trust import TrustPolicy


def _pol(tmp_path, data):
    p = tmp_path / "trust.json"
    p.write_text(json.dumps(data))
    return TrustPolicy(path=p)


def test_remote_max_role_defaults_to_speaker(tmp_path):
    pol = _pol(tmp_path, {"full_access": ["chef.skworld"], "default": "deny"})
    assert pol.remote_max_role == "speaker"          # default preserves behavior


def test_remote_max_role_can_be_listener(tmp_path):
    pol = _pol(tmp_path, {"full_access": ["chef.skworld"], "default": "deny",
                          "remote_max_role": "listener"})
    assert pol.remote_max_role == "listener"


def test_invalid_remote_max_role_falls_back_to_speaker(tmp_path):
    pol = _pol(tmp_path, {"full_access": [], "default": "deny",
                          "remote_max_role": "host"})   # host not allowed for remotes
    assert pol.remote_max_role == "speaker"
