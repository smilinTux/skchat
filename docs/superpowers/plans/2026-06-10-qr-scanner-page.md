# QR Scanner Page (webui) — Plan (task ce5fdd90)

> Receiving side of QR pairing. `POST /pair/accept` calls `skcomms.pairing.accept_pairing` (the secure verify-fingerprint-then-TOFU-add backend). `/pair/scan` page: native **BarcodeDetector** camera decode (no vendored JS — sovereign) with a **manual paste skp:// URI** fallback (genuinely useful + the testable path). Completes the round-trip with the `/pair` generator page.

**Conventions:** TDD; `~/.skenv/bin/python -m pytest ... -p no:cacheprovider`; explicit `git add`; Co-Authored-By trailer; no push; standalone tests (TestClient + monkeypatch `skcomms.pairing.accept_pairing` — no network/peer); conftest keeps `SK_DESKTOP_NOTIFY=0`.

webui (src/skchat/webui.py): `app`, `HTMLResponse`, `_PAIR_HTML`/`/pair` already exist (the generator). Add the accept endpoint + scan page alongside.

---

## Task 1: `POST /pair/accept`

**Files:** Modify `src/skchat/webui.py`. Test: `tests/test_webui_pair.py` (extend).

Accepts a scanned/pasted `skp://` URI, calls `skcomms.pairing.accept_pairing(uri)`, returns the result (or a 400 with the error on a fingerprint mismatch / unresolvable key — those raise ValueError).

- [ ] **Step 1 — failing tests** (add to `tests/test_webui_pair.py`):
```python
def test_pair_accept_ok(monkeypatch):
    import skcomms.pairing as P
    seen = {}
    def _accept(src, **kw):
        seen["src"] = src
        return {"fqid": "opus@chef.skworld", "fingerprint": "CD"*20}
    monkeypatch.setattr(P, "accept_pairing", _accept)
    r = TestClient(webui.app).post("/pair/accept", json={"uri": "skp://pair?v=1&fqid=opus@chef.skworld&fp=CDCD"})
    assert r.status_code == 200, r.text
    assert seen["src"].startswith("skp://pair?")
    assert r.json()["fqid"] == "opus@chef.skworld"

def test_pair_accept_mismatch_is_400(monkeypatch):
    import skcomms.pairing as P
    def _accept(src, **kw):
        raise ValueError("fingerprint mismatch for opus@chef.skworld — refusing to pair")
    monkeypatch.setattr(P, "accept_pairing", _accept)
    r = TestClient(webui.app).post("/pair/accept", json={"uri": "skp://pair?v=1&fqid=x&fp=00"})
    assert r.status_code == 400
    assert "mismatch" in r.json()["detail"].lower()

def test_pair_accept_missing_uri_is_400():
    r = TestClient(webui.app).post("/pair/accept", json={})
    assert r.status_code == 400
```

- [ ] **Step 2 — confirm fail** (404).

- [ ] **Step 3 — implement** in webui.py (use the existing FastAPI imports; add `Body`/`Request` if needed):
```python
@app.post("/pair/accept")
async def pair_accept(payload: dict = Body(...)):
    from skcomms import pairing
    uri = (payload or {}).get("uri", "").strip()
    if not uri:
        raise HTTPException(status_code=400, detail="missing 'uri'")
    try:
        res = pairing.accept_pairing(uri)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    asyncio.create_task(_ws_broadcast({"type": "new"}))  # peer list / a system note may change
    return res
```
(Use the module's existing `HTTPException`, `Body`, `asyncio`, `_ws_broadcast` — they're imported already from the attachments work; add `Body` to the fastapi import if missing.)

- [ ] **Step 4 — confirm pass.**
- [ ] **Step 5 — commit:** `feat(webui): POST /pair/accept — verify+TOFU-add a scanned peer` (trailer).

---

## Task 2: `GET /pair/scan` page (camera + manual paste)

**Files:** Modify `src/skchat/webui.py`. Test: `tests/test_webui_pair.py`.

- [ ] **Step 1 — failing test:**
```python
def test_pair_scan_page_wiring():
    html = TestClient(webui.app).get("/pair/scan").text
    for tok in ["/pair/accept", "BarcodeDetector", "getUserMedia", "skp://", 'id="manual"', "Pair"]:
        assert tok in html, tok
```

- [ ] **Step 2 — confirm fail.**

- [ ] **Step 3 — implement:** add `_SCAN_HTML` + a `/pair/scan` route. The page: a `<video>` camera preview (getUserMedia), uses `BarcodeDetector` (if available) to scan frames for a QR → on a decoded `skp://` value, POST `/pair/accept`; a manual `<input id="manual">` to paste an `skp://` URI + a Pair button that POSTs it; a `#result` area showing success ("Paired with …") or the error. Degrade gracefully when BarcodeDetector/camera is unavailable (show a note + keep manual paste working). ES5-safe JS in the Python string. Example:
```python
_SCAN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>skchat — Scan to Pair</title>
<style>body{font-family:system-ui;max-width:480px;margin:2rem auto;text-align:center}
video{width:300px;max-width:100%;border-radius:8px;background:#111}
#manual{width:100%;font-size:.8rem} #result{margin-top:1rem;font-weight:600}
.err{color:#b00} .ok{color:#070}</style></head>
<body><h2>Scan to pair</h2>
<video id="cam" autoplay playsinline muted></video>
<p id="camnote" style="color:#777"></p>
<p>…or paste an <code>skp://</code> link:</p>
<input id="manual" placeholder="skp://pair?v=1&fqid=…"><button id="go">Pair</button>
<div id="result"></div>
<script>
function show(msg,ok){var r=document.getElementById('result');r.textContent=msg;r.className=ok?'ok':'err';}
function accept(uri){
 if(!uri||uri.indexOf('skp://')!==0){show('Not an skp:// pairing link',false);return;}
 fetch('/pair/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uri:uri})})
  .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
  .then(function(x){show(x.ok?('Paired with '+x.d.fqid):(x.d.detail||'Pairing failed'),x.ok);});
}
document.getElementById('go').onclick=function(){accept(document.getElementById('manual').value.trim());};
(function(){
 var note=document.getElementById('camnote');
 if(!('BarcodeDetector' in window)){note.textContent='Camera QR scan not supported here — paste the link instead.';return;}
 navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}}).then(function(stream){
   var v=document.getElementById('cam');v.srcObject=stream;
   var det=new BarcodeDetector({formats:['qr_code']});var done=false;
   var tick=function(){ if(done)return; det.detect(v).then(function(codes){
     for(var i=0;i<codes.length;i++){var val=codes[i].rawValue||'';if(val.indexOf('skp://')===0){done=true;stream.getTracks().forEach(function(t){t.stop();});accept(val);return;}}
     requestAnimationFrame(tick);}).catch(function(){requestAnimationFrame(tick);});};
   requestAnimationFrame(tick);
 }).catch(function(){note.textContent='Camera unavailable — paste the link instead.';});
})();
</script></body></html>"""

@app.get("/pair/scan", response_class=HTMLResponse)
async def pair_scan_page() -> HTMLResponse:
    return HTMLResponse(_SCAN_HTML)
```

- [ ] **Step 4 — confirm pass.**
- [ ] **Step 5 — commit:** `feat(webui): /pair/scan — camera + manual-paste pairing` (trailer).

---

## Task 3: full suite green
- [ ] `~/.skenv/bin/python -m pytest tests/test_webui_pair.py -q` green; then `-m "not e2e_live"` — no new failures.
