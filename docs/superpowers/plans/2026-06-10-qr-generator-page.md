# QR Generator Page (webui) — Plan (task 0aa959f0)

> Follow-on to the QR-pairing core (skcomms `pairing.py`). Renders your pairing QR in the skchat webui with a **capability selector** = toggle which connectivity hints you share (Syncthing / Tailscale / HTTPS) + an **Embed key** toggle (offline-capable, larger QR). Reuses `skcomms.pairing.bundle_from_self` + `make_pairing_qr`/`to_skp_uri`. segno renders SVG.

**Conventions:** TDD; `~/.skenv/bin/python -m pytest ... -p no:cacheprovider`; explicit `git add`; Co-Authored-By trailer; no push; standalone tests (TestClient, monkeypatch bundle_from_self — no identity/network needed); conftest keeps `SK_DESKTOP_NOTIFY=0`.

skchat webui (src/skchat/webui.py) exposes `app` (FastAPI), `HTMLResponse`, the `_HTML` page pattern, and routes like `/legacy`.

---

## Task 1: `GET /pair/qr` JSON endpoint

**Files:** Modify `src/skchat/webui.py`. Test: `tests/test_webui_pair.py` (new).

Returns `{ "uri": "skp://pair?...", "svg": "<svg...>" }` for the selected options. Builds `skcomms.pairing.bundle_from_self(embed_key=<embed>)`, then **drops** the hints whose query flag is `0`/absent (so the user only shares the selected connectivity hints), re-renders the QR via segno SVG.

- [ ] **Step 1 — failing test** (`tests/test_webui_pair.py`):
```python
from fastapi.testclient import TestClient
from skchat import webui

def _fake_bundle():
    from skcomms.pairing import PairingBundle
    return PairingBundle(fqid="lumina@chef.skworld", fingerprint="AB"*20,
                         syncthing_device_id="DEV-9", tailscale="lumina.ts.net",
                         https="https://x/peers.json")

def test_pair_qr_default_includes_all_hints(monkeypatch):
    import skcomms.pairing as P
    monkeypatch.setattr(P, "bundle_from_self", lambda agent=None, embed_key=False: _fake_bundle())
    r = TestClient(webui.app).get("/pair/qr")
    assert r.status_code == 200
    j = r.json()
    assert j["uri"].startswith("skp://pair?")
    assert "sy=DEV-9" in j["uri"] and "ts=" in j["uri"] and "https=" in j["uri"]
    assert "<svg" in j["svg"]

def test_pair_qr_drops_deselected_hints(monkeypatch):
    import skcomms.pairing as P
    monkeypatch.setattr(P, "bundle_from_self", lambda agent=None, embed_key=False: _fake_bundle())
    r = TestClient(webui.app).get("/pair/qr?sy=0&ts=1&https=0")
    j = r.json()
    assert "sy=" not in j["uri"]      # syncthing dropped
    assert "ts=" in j["uri"]          # tailscale kept
    assert "https=" not in j["uri"]   # https dropped
    assert "fqid=" in j["uri"] and "fp=" in j["uri"]   # identity always present

def test_pair_qr_embed_key(monkeypatch):
    import skcomms.pairing as P
    def _bundle(agent=None, embed_key=False):
        b = _fake_bundle()
        if embed_key: b.pubkey = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nx\n-----END-----\n"
        return b
    monkeypatch.setattr(P, "bundle_from_self", _bundle)
    assert "pk=" in TestClient(webui.app).get("/pair/qr?embed=1").json()["uri"]
    assert "pk=" not in TestClient(webui.app).get("/pair/qr?embed=0").json()["uri"]
```

- [ ] **Step 2 — confirm fail** (404).

- [ ] **Step 3 — implement** in webui.py:
```python
@app.get("/pair/qr")
def pair_qr(sy: str = "1", ts: str = "1", https: str = "1", embed: str = "0"):
    import io
    import segno
    from skcomms import pairing
    def _on(v): return str(v).lower() not in ("0", "false", "no", "off", "")
    bundle = pairing.bundle_from_self(embed_key=_on(embed))
    if not _on(sy): bundle.syncthing_device_id = None
    if not _on(ts): bundle.tailscale = None
    if not _on(https): bundle.https = None
    uri = pairing.to_skp_uri(bundle)
    buf = io.BytesIO(); segno.make(uri, error="m").save(buf, kind="svg", scale=5)
    return {"uri": uri, "svg": buf.getvalue().decode("utf-8"),
            "fqid": bundle.fqid, "fingerprint": bundle.fingerprint}
```

- [ ] **Step 4 — confirm pass.**
- [ ] **Step 5 — commit:** `feat(webui): GET /pair/qr — pairing QR + skp:// URI for selected hints` (trailer).

---

## Task 2: `GET /pair` page (QR + capability selector)

**Files:** Modify `src/skchat/webui.py`. Test: `tests/test_webui_pair.py`.

- [ ] **Step 1 — failing test:**
```python
def test_pair_page_has_selector_and_qr():
    html = TestClient(webui.app).get("/pair").text
    for tok in ["/pair/qr", "Syncthing", "Tailscale", "HTTPS", "Embed", 'id="pair-qr"']:
        assert tok in html, tok
```

- [ ] **Step 2 — confirm fail.**

- [ ] **Step 3 — implement:** add a `/pair` HTML route serving a page with: a `#pair-qr` container (initial QR fetched on load), a `#pair-uri` line (the skp:// text + a Copy button), and a capability-selector form — checkboxes `Syncthing` / `Tailscale` / `HTTPS` (checked) + `Embed public key (offline)` (unchecked). JS re-fetches `/pair/qr?sy=&ts=&https=&embed=` on any toggle change and swaps in the returned `svg` + `uri`. Mirror the `_HTML`/`HTMLResponse` style; keep the JS ES5-safe inside the Python string. Example:
```python
_PAIR_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>skchat — Pair</title>
<style>body{font-family:system-ui;max-width:480px;margin:2rem auto;text-align:center}
#pair-qr svg{width:280px;height:280px} label{display:block;text-align:left;margin:.3rem 0}
#pair-uri{word-break:break-all;font-size:.8rem;color:#555;margin-top:1rem}</style></head>
<body><h2>Pair a device</h2><p>Scan this with another agent's <code>skchat</code>.</p>
<div id="pair-qr">loading…</div>
<form id="caps">
 <label><input type="checkbox" name="sy" checked> Share Syncthing device</label>
 <label><input type="checkbox" name="ts" checked> Share Tailscale address</label>
 <label><input type="checkbox" name="https" checked> Share HTTPS endpoint</label>
 <label><input type="checkbox" name="embed"> Embed public key (offline-capable, bigger QR)</label>
</form>
<div id="pair-uri"></div><button id="copy">Copy link</button>
<script>
function flag(n){return document.querySelector('input[name='+n+']').checked?'1':'0';}
function refresh(){
 var q='/pair/qr?sy='+flag('sy')+'&ts='+flag('ts')+'&https='+flag('https')+'&embed='+flag('embed');
 fetch(q).then(function(r){return r.json();}).then(function(d){
   document.getElementById('pair-qr').innerHTML=d.svg;
   document.getElementById('pair-uri').textContent=d.uri;
   window._uri=d.uri;});
}
document.getElementById('caps').addEventListener('change',refresh);
document.getElementById('copy').onclick=function(){if(window._uri)navigator.clipboard.writeText(window._uri);};
refresh();
</script></body></html>"""

@app.get("/pair", response_class=HTMLResponse)
async def pair_page() -> HTMLResponse:
    return HTMLResponse(_PAIR_HTML)
```

- [ ] **Step 4 — confirm pass.**
- [ ] **Step 5 — commit:** `feat(webui): /pair page — QR + capability selector` (trailer).

---

## Task 3: full webui suite green
- [ ] `~/.skenv/bin/python -m pytest tests/ -k "webui or pair" -q -p no:cacheprovider` green; then `-m "not e2e_live"` confirms no new failures.
