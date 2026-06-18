"""Markup + page-route tests for the Sovereign Conf Calls web client (conf.html).

Mirrors tests/test_spaces_page.py (route serves 200 HTML) and
tests/test_spaces_ui_markup.py (assert the proven media JS + conf wiring is in
the static file). No SFU is touched — the page route is pure static serving.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.conf.room import ConfRegistry
from skchat.conf.routes import register_conf_routes


def _html() -> str:
    # Resolve relative to this test file, not the CWD — the repo convention is to
    # run pytest from ~ (avoids the skmemory namespace collision), where a bare
    # "src/..." relative path does not resolve.
    p = Path(__file__).resolve().parent.parent / "src" / "skchat" / "static" / "conf.html"
    return p.read_text(encoding="utf-8")


# ── Page route ──────────────────────────────────────────────────────────────────
def test_conf_page_served(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_conf_routes(app, registry=ConfRegistry(path=tmp_path / "c.json"))
    c = TestClient(app)
    r = c.get("/conf/conf-anything0000")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text.lower()
    assert "conference" in body


def test_conf_page_route_does_not_shadow_list(tmp_path, monkeypatch):
    # GET /conf (list) and GET /conf/{room} (page) must coexist.
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_conf_routes(app, registry=ConfRegistry(path=tmp_path / "c.json"))
    c = TestClient(app)
    listing = c.get("/conf")
    assert listing.status_code == 200
    assert "confs" in listing.json()


# ── Media core markup (lifted from livekit.html) ────────────────────────────────
def test_video_grid_present():
    html = _html()
    assert 'id="grid"' in html
    assert "class=\"tile\"" in html or "'tile'" in html


def test_screenshare_button_and_publish():
    html = _html()
    assert 'id="screen-share-btn"' in html
    # the verbatim getDisplayMedia → ScreenShare publish path
    assert "getDisplayMedia" in html
    assert "Track.Source.ScreenShare" in html


def test_mute_and_cam_toggles():
    html = _html()
    assert 'id="mic-toggle"' in html
    assert 'id="cam-toggle"' in html
    assert "setMicrophoneEnabled" in html
    assert "setCameraEnabled" in html


# ── Conf-specific wiring ────────────────────────────────────────────────────────
def test_token_fetched_from_conf_route():
    html = _html()
    # NOT /livekit/token — the conf client mints via /conf/{room}/token.
    assert "/conf/" in html
    assert "/token" in html
    assert "/livekit/config" in html  # ws url still comes from here


def test_roster_bound_to_participants_endpoint():
    html = _html()
    assert "/participants" in html
    assert 'id="roster"' in html


def test_active_speaker_highlight():
    html = _html()
    assert "ActiveSpeakersChanged" in html
    assert "speaking" in html


def test_host_end_call_wired():
    html = _html()
    assert 'id="end-call"' in html
    assert "/end" in html


def test_room_from_url_not_manual_input():
    html = _html()
    # conf id comes from the URL path/param, not a manual dev <input>.
    assert "location.pathname" in html
    assert "/conf/" in html
