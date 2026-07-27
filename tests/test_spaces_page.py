import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes

_BUILD_HASH_RE = re.compile(r"^[0-9a-f]{12}$")


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


def test_space_page_build_stamp_injected(tmp_path, monkeypatch):
    # VER: an already-open Space tab keeps running stale JS across a deploy
    # (no server no-cache header helps a tab that never reloads). The page
    # must carry a real build hash in place of the __SPACE_BUILD__
    # placeholder so client-side JS can detect a newer deploy landed.
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    c = TestClient(app)
    r = c.get("/space/space-anything0000000")
    assert r.status_code == 200
    assert "__SPACE_BUILD__" not in r.text
    m = re.search(r'const SPACE_BUILD = "([0-9a-f]{12})";', r.text)
    assert m, "expected a 12-hex build hash substituted into SPACE_BUILD"
    # Still no-cache: the build stamp does not replace the deploy hardening.
    cc = r.headers.get("cache-control", "")
    assert "no-cache" in cc and "no-store" in cc


def test_spaces_build_endpoint_matches_injected_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    c = TestClient(app)
    page = c.get("/space/space-anything0000000").text
    injected = re.search(r'const SPACE_BUILD = "([0-9a-f]{12})";', page).group(1)

    r = c.get("/spaces/build")
    assert r.status_code == 200
    body = r.json()
    assert _BUILD_HASH_RE.match(body["build"])
    assert body["build"] == injected
