from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


def test_directory_page_served(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    c = TestClient(app)
    r = c.get("/spaces/live")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # the page fetches the live list from the JSON endpoint
    assert "/spaces" in r.text


def test_directory_escapes_user_fields(tmp_path, monkeypatch):
    """C1: the served HTML must define an escaping helper and use it for the
    attacker-settable title/host_fqid fields (defense against stored XSS)."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    c = TestClient(app)
    html = c.get("/spaces/live").text
    # an escaping helper is defined and the HTML-entity replace map is present
    assert "esc(" in html
    assert "&amp;" in html
    # both user-controlled fields are escaped, not interpolated raw
    assert "esc(s.title)" in html
    assert "esc(s.host_fqid)" in html
    assert "${s.title}" not in html
    assert "${s.host_fqid}" not in html
