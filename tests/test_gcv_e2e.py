"""Unit tests for the browser-free logic of ``scripts/gcv_e2e.py``.

This harness DRIVES live browsers for the E2E phase; those legs are not exercised
here. What IS unit-tested is every pure helper the harness relies on -- url
building, the guest-redirect reconstruction, the sovereign-TURN ICE assertion,
the safe Chrome flag builder (forbidden-profile / forbidden-port guards), the
CDP /json/version parse, and the scenario selector -- so a regression in the
load-bearing logic fails fast without a browser or a live host.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load scripts/gcv_e2e.py as a module (scripts/ is not a package). It must be
# registered in sys.modules BEFORE exec so dataclasses can resolve the module's
# own forward-referenced annotations (from __future__ import annotations).
_MOD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gcv_e2e.py"
_spec = importlib.util.spec_from_file_location("gcv_e2e", _MOD_PATH)
gcv = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
sys.modules["gcv_e2e"] = gcv
_spec.loader.exec_module(gcv)


# --------------------------------------------------------------------------- #
# URL builders
# --------------------------------------------------------------------------- #

def test_funnel_join_url():
    u = gcv.funnel_join_url("https://host:10000/", "call-abc", "tok.en/with+chars")
    assert u.startswith("https://host:10000/join/call-abc?invite=")
    # invite must be percent-encoded (no raw '/' or '+').
    assert "tok.en%2Fwith%2Bchars" in u


def test_conf_page_url():
    u = gcv.conf_page_url("https://host:10000", "conf-1", "lumina@chef.skworld")
    assert u == "https://host:10000/conf/conf-1?identity=lumina%40chef.skworld"


def test_livekit_page_url_has_all_params():
    u = gcv.livekit_page_url("https://h", "room-x", "guest:abc", "jwt.tok.en")
    assert u.startswith("https://h/livekit/room-x?")
    assert "room=room-x" in u
    assert "identity=guest%3Aabc" in u
    assert "token=jwt.tok.en" in u


def test_build_guest_redirect_url_matches_joinhtml_contract():
    resp = {"room": "call-z", "identity": "guest:1234", "lk_token": "AAA.BBB.CCC"}
    u = gcv.build_guest_redirect_url("https://h", resp)
    # join.html redirects to /livekit/<room>?room=&identity=&token=<lk_token>
    assert u.startswith("https://h/livekit/call-z?")
    assert "token=AAA.BBB.CCC" in u
    assert "identity=guest%3A1234" in u


# --------------------------------------------------------------------------- #
# ICE / sovereign-TURN assertion (scenario d core)
# --------------------------------------------------------------------------- #

_SOVEREIGN_ICE = {
    "ice_servers": [
        {"urls": ["stun:stun.l.google.com:19302"]},
        {
            "urls": [
                "turn:noroc2027.tail204f0c.ts.net:443",
                "turns:noroc2027.tail204f0c.ts.net:443?transport=tcp",
            ],
            "username": "1700000000:guest@public",
            "credential": "abc==",
        },
    ],
    "policy": "all",
    "preferred_tier": 3,
    "on_tailnet": False,
}

_OPENRELAY_ICE = {
    "ice_servers": [
        {"urls": ["stun:stun.l.google.com:19302"]},
        {
            "urls": [
                "turn:openrelay.metered.ca:80",
                "turn:openrelay.metered.ca:443",
                "turn:openrelay.metered.ca:443?transport=tcp",
            ],
            "username": "openrelayproject",
            "credential": "openrelayproject",
        },
    ],
    "policy": "all",
    "preferred_tier": 3,
}

_TAILNET_ICE = {"ice_servers": [], "policy": "all", "preferred_tier": 1, "on_tailnet": True}


def test_extract_turn_urls_flattens_and_filters():
    urls = gcv.extract_turn_urls(_SOVEREIGN_ICE)
    assert "turn:noroc2027.tail204f0c.ts.net:443" in urls
    assert all(u.startswith(("turn:", "turns:")) for u in urls)
    # stun urls are excluded
    assert not any("stun:" in u for u in urls)


def test_assert_sovereign_turn_pass():
    ok, ev = gcv.assert_sovereign_turn(_SOVEREIGN_ICE)
    assert ok is True
    assert ev["has_sovereign_turn"] is True
    assert ev["has_openrelay"] is False


def test_assert_sovereign_turn_rejects_openrelay():
    ok, ev = gcv.assert_sovereign_turn(_OPENRELAY_ICE)
    assert ok is False
    assert ev["has_openrelay"] is True
    assert ev["has_sovereign_turn"] is False


def test_assert_sovereign_turn_rejects_empty_tailnet():
    ok, ev = gcv.assert_sovereign_turn(_TAILNET_ICE)
    assert ok is False
    assert ev["turn_urls"] == []


def test_assert_sovereign_turn_wrong_port_fails():
    cfg = {"ice_servers": [{"urls": ["turn:noroc2027.tail204f0c.ts.net:3478"]}]}
    ok, _ = gcv.assert_sovereign_turn(cfg, turn_port=443)
    assert ok is False


def test_assert_sovereign_turn_string_urls_field():
    # iceServers[].urls may be a bare string, not a list.
    cfg = {"ice_servers": [{"urls": "turn:noroc2027.tail204f0c.ts.net:443"}]}
    ok, _ = gcv.assert_sovereign_turn(cfg)
    assert ok is True


# --------------------------------------------------------------------------- #
# Chrome flag builder -- SAFETY guards
# --------------------------------------------------------------------------- #

def test_build_chrome_flags_contains_required():
    flags = gcv.build_chrome_flags("/tmp/cdp-gcv-a-1", 9250, "https://x/join/r")
    assert "--headless=new" in flags
    assert "--use-fake-device-for-media-stream" in flags
    assert "--use-fake-ui-for-media-stream" in flags
    assert "--remote-debugging-port=9250" in flags
    assert "--user-data-dir=/tmp/cdp-gcv-a-1" in flags
    assert flags[-1] == "https://x/join/r"


def test_build_chrome_flags_refuses_forbidden_port():
    with pytest.raises(ValueError):
        gcv.build_chrome_flags("/tmp/cdp-gcv-a-1", gcv.FORBIDDEN_PORT)


def test_build_chrome_flags_refuses_forbidden_profile():
    with pytest.raises(ValueError):
        gcv.build_chrome_flags(gcv.FORBIDDEN_PROFILE, 9250)


def test_role_ports_are_dedicated_and_not_9229():
    assert gcv.cdp_role_port("A") == 9250
    assert gcv.cdp_role_port("b") == 9251
    assert gcv.FORBIDDEN_PORT not in gcv.ROLE_PORTS.values()


def test_cdp_role_port_unknown():
    with pytest.raises(ValueError):
        gcv.cdp_role_port("Z")


# --------------------------------------------------------------------------- #
# CDP endpoint parse
# --------------------------------------------------------------------------- #

def test_parse_ws_endpoint():
    payload = {"Browser": "HeadlessChrome/1", "webSocketDebuggerUrl": "ws://127.0.0.1:9250/devtools/browser/xyz"}
    assert gcv.parse_ws_endpoint(payload) == "ws://127.0.0.1:9250/devtools/browser/xyz"


def test_parse_ws_endpoint_missing():
    with pytest.raises(RuntimeError):
        gcv.parse_ws_endpoint({"Browser": "x"})


# --------------------------------------------------------------------------- #
# Scenario selector
# --------------------------------------------------------------------------- #

def test_resolve_scenarios_all():
    assert gcv.resolve_scenarios("all") == ["a", "b", "c", "d"]


def test_resolve_scenarios_names_and_letters():
    assert gcv.resolve_scenarios("guest-join-conf,d") == ["a", "d"]
    assert gcv.resolve_scenarios("b, c") == ["b", "c"]


def test_resolve_scenarios_dedup():
    assert gcv.resolve_scenarios("a,a,turn-path,d") == ["a", "d"]


def test_resolve_scenarios_unknown():
    with pytest.raises(ValueError):
        gcv.resolve_scenarios("zz")


# --------------------------------------------------------------------------- #
# API wrappers build the right requests (patched transport, no live host)
# --------------------------------------------------------------------------- #

def test_mint_guest_invite_sends_operator_bearer(monkeypatch):
    captured = {}

    def fake_request(method, url, *, body=None, headers=None, timeout=15.0):
        captured.update(method=method, url=url, body=body, headers=headers)
        return {"invite_token": "tok", "jti": "deadbeef"}

    monkeypatch.setattr(gcv, "_request", fake_request)
    out = gcv.mint_guest_invite("https://h", "call-x", operator_token="s3cr3t", display="g")
    assert out["invite_token"] == "tok"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://h/guest/invite"
    assert captured["headers"]["Authorization"] == "Bearer s3cr3t"
    assert captured["body"] == {"room": "call-x", "display": "g", "single_use": False}


def test_mint_conf_omits_slug_when_absent(monkeypatch):
    captured = {}

    def fake_request(method, url, *, body=None, headers=None, timeout=15.0):
        captured.update(url=url, body=body)
        return {"room": "conf-1", "token": "t"}

    monkeypatch.setattr(gcv, "_request", fake_request)
    gcv.mint_conf("https://h", "lumina@chef.skworld", "Title")
    assert captured["url"] == "https://h/conf/create"
    assert "slug" not in captured["body"]
    assert captured["body"]["host_fqid"] == "lumina@chef.skworld"


def test_fetch_ice_encodes_peer(monkeypatch):
    captured = {}

    def fake_request(method, url, *, body=None, headers=None, timeout=15.0):
        captured.update(url=url)
        return dict(_SOVEREIGN_ICE)

    monkeypatch.setattr(gcv, "_request", fake_request)
    gcv.fetch_ice("https://h", "guest@public")
    assert captured["url"] == "https://h/connectivity/ice?peer=guest%40public"


def test_scenario_turn_path_pass(monkeypatch):
    monkeypatch.setattr(gcv, "fetch_ice", lambda base, peer: dict(_SOVEREIGN_ICE))
    res = gcv.scenario_turn_path("https://h", turn_host=gcv.DEFAULT_TURN_HOST, turn_port=443, peer="p")
    assert res.passed is True
    assert res.name == "turn-path"


def test_scenario_turn_path_fail_openrelay(monkeypatch):
    monkeypatch.setattr(gcv, "fetch_ice", lambda base, peer: dict(_OPENRELAY_ICE))
    res = gcv.scenario_turn_path("https://h", turn_host=gcv.DEFAULT_TURN_HOST, turn_port=443, peer="p")
    assert res.passed is False
    assert res.error
