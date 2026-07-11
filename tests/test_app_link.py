"""Unit tests for the native-app conf deep-link helpers (coord 59184ca7)."""

from types import SimpleNamespace

from skchat.app_link import conf_app_link, wants_web_fallback


def test_conf_app_link_room_only():
    link = conf_app_link("conf-abc")
    assert link == "/app/#/conf?room=conf-abc"


def test_conf_app_link_carries_pre_minted_credential():
    link = conf_app_link(
        "conf-abc",
        token="jwt-123",
        url="wss://lk.test/ws",
        identity="guest:deadbeef",
        display="Alice",
    )
    assert link.startswith("/app/#/conf?")
    assert "room=conf-abc" in link
    assert "token=jwt-123" in link
    # url + identity are URL-encoded into the query string.
    assert "url=wss%3A%2F%2Flk.test%2Fws" in link
    assert "identity=guest%3Adeadbeef" in link
    assert "display=Alice" in link


def test_conf_app_link_omits_empty_fields():
    link = conf_app_link("conf-abc", token="", url="", identity="", display="")
    assert link == "/app/#/conf?room=conf-abc"


def test_wants_web_fallback_reads_flag():
    assert wants_web_fallback(SimpleNamespace(query_params={"web": "1"})) is True
    assert wants_web_fallback(SimpleNamespace(query_params={"web": "0"})) is False
    assert wants_web_fallback(SimpleNamespace(query_params={})) is False


def test_wants_web_fallback_defensive_on_bad_request():
    assert wants_web_fallback(object()) is False
