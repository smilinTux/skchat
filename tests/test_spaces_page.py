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
