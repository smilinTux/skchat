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
    once = webui._rewrite_html_asset_prefix(
        b'<link href="/static/css/board.css">', "/skdashboard"
    )
    twice = webui._rewrite_html_asset_prefix(once, "/skdashboard")
    assert once == twice
    assert b"/skdashboard/skdashboard" not in twice


def test_rewrite_leaves_non_asset_root_paths_alone():
    # A non-static/assets root-absolute nav link is not an asset load; leave it.
    html = b'<a href="/board">Board</a><link href="/static/css/board.css">'
    out = webui._rewrite_html_asset_prefix(html, "/skdashboard")
    assert b'<a href="/board">' in out
    assert b'href="/skdashboard/static/css/board.css"' in out


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
