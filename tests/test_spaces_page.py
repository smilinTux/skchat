from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


def test_space_page_served(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    c = TestClient(app)
    r = c.get("/space/space-anything0000000")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "livekit" in r.text.lower()
    # The HTML shell must never be cached, or a phone runs stale client JS
    # across deploys (this hid the guest unmute button after a promotion).
    assert "no-store" in r.headers.get("cache-control", "")


def test_space_page_not_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    c = TestClient(app)
    for path in ("/space/space-anything0000000", "/spaces/live"):
        cc = c.get(path).headers.get("cache-control", "")
        assert "no-cache" in cc and "no-store" in cc, path
