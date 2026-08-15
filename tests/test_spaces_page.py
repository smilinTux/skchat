import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes

_BUILD_HASH_RE = re.compile(r"^[0-9a-f]{12}$")

# The legacy standalone client. `/space/{id}` redirects to the Flutter app now
# (see test_space_link_redirects_to_the_app), so every test below that is
# actually about space.html asks for it explicitly.
_LEGACY = "/space/space-anything0000000?legacy=1"


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    return TestClient(app)


def test_space_link_redirects_to_the_app(tmp_path, monkeypatch):
    # A shared Space link used to serve space.html, a DIFFERENT and older
    # client with no Watch Together in it: guests joined the right room and
    # then looked at an app with no video. The app-side share sheet builds the
    # right URL now, but only this redirect heals the links already sent.
    c = _client(tmp_path, monkeypatch)
    r = c.get("/space/space-anything0000000", follow_redirects=False)
    assert r.status_code == 302
    # The `#` is load-bearing: the Flutter app is mounted at /app/ and calls no
    # usePathUrlStrategy, so it runs on Flutter web's default HASH strategy.
    # Without the fragment the SPA catch-all serves index.html and the router
    # boots with an empty route, landing the guest on the home screen.
    assert r.headers["location"] == "/app/#/spaces/space-anything0000000"
    assert "no-store" in r.headers.get("cache-control", "")


def test_space_link_redirect_escapes_the_space_id(tmp_path, monkeypatch):
    # The id lands in a Location header, so it goes through quote() rather
    # than straight into the URL: an id is untrusted path input and must not
    # be able to contribute structure (another `#`, a `/`, a stray space) to
    # the URL it is interpolated into.
    c = _client(tmp_path, monkeypatch)
    r = c.get("/space/space-a%20b%23c", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/app/#/spaces/space-a%20b%23c"


def test_legacy_space_page_still_served_on_request(tmp_path, monkeypatch):
    # The old page is the only client that works at all if the Flutter build
    # is missing or broken. Turning a working fallback into a 404 to fix a
    # link would be a bad trade.
    c = _client(tmp_path, monkeypatch)
    r = c.get(_LEGACY)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "livekit" in r.text.lower()
    # The HTML shell must never be cached, or a phone runs stale client JS
    # across deploys (this hid the guest unmute button after a promotion).
    assert "no-store" in r.headers.get("cache-control", "")


def test_space_page_not_cached(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    for path in (_LEGACY, "/spaces/live"):
        cc = c.get(path).headers.get("cache-control", "")
        assert "no-cache" in cc and "no-store" in cc, path


def test_space_page_build_stamp_injected(tmp_path, monkeypatch):
    # VER: an already-open Space tab keeps running stale JS across a deploy
    # (no server no-cache header helps a tab that never reloads). The page
    # must carry a real build hash in place of the __SPACE_BUILD__
    # placeholder so client-side JS can detect a newer deploy landed.
    c = _client(tmp_path, monkeypatch)
    r = c.get(_LEGACY)
    assert r.status_code == 200
    assert "__SPACE_BUILD__" not in r.text
    m = re.search(r'const SPACE_BUILD = "([0-9a-f]{12})";', r.text)
    assert m, "expected a 12-hex build hash substituted into SPACE_BUILD"
    # Still no-cache: the build stamp does not replace the deploy hardening.
    cc = r.headers.get("cache-control", "")
    assert "no-cache" in cc and "no-store" in cc


def test_spaces_build_endpoint_matches_injected_hash(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    page = c.get(_LEGACY).text
    injected = re.search(r'const SPACE_BUILD = "([0-9a-f]{12})";', page).group(1)

    r = c.get("/spaces/build")
    assert r.status_code == 200
    body = r.json()
    assert _BUILD_HASH_RE.match(body["build"])
    assert body["build"] == injected
