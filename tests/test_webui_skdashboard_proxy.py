"""skdashboard funnel proxy: asset-prefix rewriting for the embedded Board pane.

The skcapstone coordination dashboard references its CSS/JS by ABSOLUTE path
(``/static/css/board.css``, ``/static/js/cmdb.js``). Served raw through the
``/skdashboard`` reverse-proxy prefix, the browser would request
``<origin>/static/...`` and hit the SHELL's own Flutter ``/static`` mount, not the
dashboard's, so the embedded pane renders blank. These tests assert the proxy
rewrites those root-absolute asset URLs onto ``/skdashboard/static`` (and
``/assets``) in ``text/html`` bodies, that the rewrite is idempotent (no
double-prefix), and that non-HTML bodies are left byte-for-byte untouched.

The upstream ``urlopen`` is monkeypatched so the tests never touch a socket. Run
from ~ per skchat/CLAUDE.md (skmemory namespace collision).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from skchat import webui


# --------------------------------------------------------------------------- #
# Pure helper: _rewrite_html_asset_prefix
# --------------------------------------------------------------------------- #
def test_rewrite_prefixes_static_and_assets_hrefs_and_srcs():
    html = (
        b'<link rel="stylesheet" href="/static/css/board.css">'
        b'<script type="module" src="/static/js/cmdb.js"></script>'
        b"<img src='/assets/logo.png'>"
    )
    out = webui._rewrite_html_asset_prefix(html, "/skdashboard")
    assert b'href="/skdashboard/static/css/board.css"' in out
    assert b'src="/skdashboard/static/js/cmdb.js"' in out
    assert b"src='/skdashboard/assets/logo.png'" in out
    # No un-prefixed /static or /assets asset attr survives.
    assert b'href="/static/' not in out
    assert b'src="/static/' not in out
    assert b"src='/assets/" not in out


def test_rewrite_is_idempotent_no_double_prefix():
    once = webui._rewrite_html_asset_prefix(b'<link href="/static/css/board.css">', "/skdashboard")
    twice = webui._rewrite_html_asset_prefix(once, "/skdashboard")
    assert once == twice
    assert b"/skdashboard/skdashboard" not in twice


def test_rewrite_prefixes_root_absolute_nav_links():
    # Root-absolute NAV links (not just assets) must be reparented onto the prefix,
    # else a nav click navigates the iframe OUT of /skdashboard into skchat.
    html = (
        b'<a href="/board">Board</a><a href="/cockpit">Cockpit</a>'
        b"<a href='/'>Home</a><link href=\"/static/css/board.css\">"
    )
    out = webui._rewrite_html_asset_prefix(html, "/skdashboard")
    assert b'<a href="/skdashboard/board">' in out
    assert b'<a href="/skdashboard/cockpit">' in out
    assert b"<a href='/skdashboard/'>" in out
    assert b'href="/skdashboard/static/css/board.css"' in out


def test_rewrite_leaves_protocol_relative_and_absolute_urls_alone():
    # Cross-origin references must never be reparented onto the local prefix.
    html = (
        b'<script src="//cdn.example.com/x.js"></script>'
        b'<a href="https://example.com/docs">docs</a>'
        b'<img src="/logo.png">'
    )
    out = webui._rewrite_html_asset_prefix(html, "/skdashboard")
    assert b'src="//cdn.example.com/x.js"' in out
    assert b'href="https://example.com/docs"' in out
    # ...but the local root-absolute one is still reparented.
    assert b'src="/skdashboard/logo.png"' in out
    assert b"/skdashboard//cdn" not in out


def test_rewrite_nav_is_idempotent():
    once = webui._rewrite_html_asset_prefix(b'<a href="/board">b</a>', "/skdashboard")
    twice = webui._rewrite_html_asset_prefix(once, "/skdashboard")
    assert once == twice
    assert b"/skdashboard/skdashboard" not in twice


# --------------------------------------------------------------------------- #
# Pure helper: fetch/XHR shim + injection
# --------------------------------------------------------------------------- #
def test_embed_fetch_shim_carries_prefix_and_token():
    shim = webui._embed_fetch_shim("/skdashboard", "tok-abc.123")
    assert shim.startswith(b"<script>") and shim.endswith(b"</script>")
    # Prefix + token embedded as JS string literals; fetch + XHR both patched.
    assert b'"/skdashboard"' in shim
    assert b'"tok-abc.123"' in shim
    assert b"window.fetch=" in shim
    assert b"XMLHttpRequest.prototype.open=" in shim
    assert b"embed_token=" in shim


def test_embed_shim_injected_after_head_once():
    html = b"<!doctype html><html><head><title>x</title></head><body></body></html>"
    out = webui._inject_embed_shim(html, "/skdashboard", "tok")
    # Injected right after <head>, before the page's own scripts.
    assert out.index(webui._EMBED_SHIM_MARKER) == out.index(b"<head>") + len(b"<head>")
    # Idempotent: re-injecting the transformed body does not double-inject.
    twice = webui._inject_embed_shim(out, "/skdashboard", "tok")
    assert twice == out
    assert twice.count(webui._EMBED_SHIM_MARKER) == 1


def test_embed_shim_prepends_when_no_head():
    out = webui._inject_embed_shim(b"<body>hi</body>", "/skdashboard", "")
    assert out.startswith(webui._EMBED_SHIM_MARKER)


# --------------------------------------------------------------------------- #
# Route: HTML bodies rewritten, non-HTML bodies untouched
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, body: bytes, status: int = 200, ctype: str = "text/html"):
        self._body = body
        self.status = status
        self.headers = {"content-type": ctype}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _gate_off(monkeypatch):
    # Keep the plane-wide auth gate off so the proxy route is reachable in-test;
    # the asset rewrite is orthogonal to auth.
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    monkeypatch.setenv("SKDASHBOARD_URL", "http://127.0.0.1:7778")


def test_route_rewrites_html_asset_urls(monkeypatch):
    dashboard_html = (
        b"<!doctype html><html><head>"
        b'<link rel="stylesheet" href="/static/css/board.css">'
        b'<script type="module" src="/static/js/cmdb.js"></script>'
        b"</head><body></body></html>"
    )

    def _fake_urlopen(req, timeout=10):
        return _FakeResp(dashboard_html, ctype="text/html; charset=utf-8")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = TestClient(webui.app)
    r = client.get("/skdashboard/overview")
    assert r.status_code == 200
    body = r.content
    assert b'href="/skdashboard/static/css/board.css"' in body
    assert b'src="/skdashboard/static/js/cmdb.js"' in body
    assert b'href="/static/' not in body
    assert b'src="/static/' not in body


def test_route_leaves_non_html_untouched(monkeypatch):
    # The CSS itself (or JSON, JS, images) must pass through byte-for-byte: only
    # text/html gets the asset-attr rewrite.
    css = b'.tab[href="/static/x"]{color:red}'

    def _fake_urlopen(req, timeout=10):
        return _FakeResp(css, ctype="text/css")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = TestClient(webui.app)
    r = client.get("/skdashboard/static/css/board.css")
    assert r.status_code == 200
    assert r.content == css


# --------------------------------------------------------------------------- #
# Full embed-token flow: mint -> navigate -> shim carries token -> data fetch
# authorizes cross-origin (CORS) with the token in the URL.
# --------------------------------------------------------------------------- #
def test_route_injects_shim_with_token_and_cors(monkeypatch):
    from skchat import embed_auth

    # A real, verifiable module-scoped read-only token (bypasses the operator-auth
    # gate on the mint route by minting directly, which is orthogonal to the proxy).
    monkeypatch.setenv("SKCHAT_EMBED_TOKEN_SECRET", "test-embed-secret")
    token, _exp = embed_auth.mint_embed_token("skdashboard")

    dashboard_html = (
        b"<!doctype html><html><head>"
        b'<link rel="stylesheet" href="/static/css/board.css">'
        b'<a href="/cockpit">Cockpit</a>'
        b'<script type="module" src="/static/js/board.js"></script>'
        b"</head><body></body></html>"
    )

    def _fake_urlopen(req, timeout=10):
        return _FakeResp(dashboard_html, ctype="text/html; charset=utf-8")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = TestClient(webui.app)
    r = client.get(f"/skdashboard/?embed_token={token}")
    assert r.status_code == 200
    body = r.content
    # Nav + assets reparented onto the prefix.
    assert b'href="/skdashboard/static/css/board.css"' in body
    assert b'<a href="/skdashboard/cockpit">' in body
    # The fetch/XHR shim is present and carries THIS token, so in-pane API calls
    # authorize cross-origin without a cookie.
    assert webui._EMBED_SHIM_MARKER in body
    assert token.encode() in body
    assert b"window.fetch=" in body
    # Opaque-origin pane can READ the reply.
    assert r.headers.get("access-control-allow-origin") == "*"
    # The proxy also sets the path-scoped cookie for the asset/nav subresource loads.
    assert embed_auth.cookie_name("skdashboard") in r.headers.get("set-cookie", "")


def test_route_data_fetch_authorizes_with_query_token(monkeypatch):
    # A data call the shim would produce: /skdashboard/api/...?embed_token=<t>.
    # The proxy must authorize it (read-only GET) and forward to the dashboard API,
    # returning the JSON with the CORS header so the opaque-origin pane can read it.
    from skchat import embed_auth

    monkeypatch.setenv("SKCHAT_EMBED_TOKEN_SECRET", "test-embed-secret")
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")  # gate ON: token is the only key
    token, _exp = embed_auth.mint_embed_token("skdashboard")

    seen = {}

    def _fake_urlopen(req, timeout=10):
        seen["url"] = req.full_url
        return _FakeResp(b'{"cards":[]}', ctype="application/json")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = TestClient(webui.app)
    r = client.get(f"/skdashboard/api/board?embed_token={token}")
    assert r.status_code == 200
    assert r.json() == {"cards": []}
    # JSON body passes through untouched (no shim, no rewrite).
    assert r.content == b'{"cards":[]}'
    assert r.headers.get("access-control-allow-origin") == "*"
    # Reached the real dashboard API upstream, under /api/board.
    assert "/api/board" in seen["url"]


def test_route_data_fetch_without_token_is_gated(monkeypatch):
    # Same data path, no token, plane-wide gate ON -> 401 (leak stays closed).
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")

    def _fake_urlopen(req, timeout=10):  # pragma: no cover - must never be reached
        raise AssertionError("upstream must not be hit without auth")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = TestClient(webui.app)
    r = client.get("/skdashboard/api/board")
    assert r.status_code == 401
