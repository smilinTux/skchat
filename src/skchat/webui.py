"""SKChat Web UI — minimal FastAPI + HTMX chat interface.

Usage:  skchat webui [--port 8765] [--no-browser]
"""

from __future__ import annotations

import asyncio
import json
import os
import re as _re
import uuid as _uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import (
    Body,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from . import __version__

import logging
logger = logging.getLogger(__name__)

app = FastAPI(title="SKChat Web UI")
_SKCHAT_HOME = Path("~/.skchat").expanduser()

# Upload size cap for the /upload endpoint (100 MiB).
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# Register voice routes — prefer the SKVoice proxy (voice_ws_lite) which delegates
# the full STT/LLM/TTS chain to the skvoice service. Fall back to the legacy
# voice_stream (in-process pipeline) only if the lite module fails to import or
# SKCHAT_VOICE_MODE=local is set.
_voice_mode = os.getenv("SKCHAT_VOICE_MODE", "proxy").lower()
_voice_routes_loaded = False

if _voice_mode != "local":
    try:
        from .voice_ws_lite import register_voice_routes_lite as _register_voice_routes_lite

        _register_voice_routes_lite(app)
        _voice_routes_loaded = True
    except ImportError:
        pass

if not _voice_routes_loaded:
    try:
        from .voice_stream import register_voice_routes as _register_voice_routes

        _register_voice_routes(app)
        _voice_routes_loaded = True
    except ImportError:
        _voice_routes_loaded = False

# FaceTime routes — aiortc/MuseTalk path (existing, fallback for non-LiveKit clients).
try:
    from .facetime import register_facetime_routes as _register_facetime_routes

    _register_facetime_routes(app)
except ImportError:
    pass

# LiveKit routes — primary video stack (token endpoint + room signalling helper).
try:
    from .livekit_routes import register_livekit_routes as _register_livekit_routes
    _register_livekit_routes(app)
except ImportError as _e:
    logger.warning("livekit routes not registered: %s", _e)
try:
    from .call_routes import register_call_routes as _register_call_routes
    _register_call_routes(app)
except ImportError as _e:
    logger.warning("call routes not registered: %s", _e)
try:
    from .spaces.routes import register_spaces_routes as _register_spaces_routes
    _register_spaces_routes(app)
except ImportError as _e:
    logger.warning("spaces routes not registered: %s", _e)
try:
    from .glossa_mesh.routes import register_glossa_routes as _register_glossa_routes
    _register_glossa_routes(app)
except ImportError as _e:
    logger.warning("glossa routes not registered: %s", _e)


@app.get("/health")
async def health() -> JSONResponse:
    """Health check endpoint for container orchestration.

    Includes the resolved agent name and current OOF level so a swarm
    healthcheck or external monitor can spot identity-drift or stuck-FEB
    bugs without scraping a separate endpoint.
    """
    try:
        from .agent_profile import get_active_agent_name, load_feb_state

        agent = get_active_agent_name()
        feb = load_feb_state(agent)
        return JSONResponse(
            {
                "status": "ok",
                "service": "skchat-webui",
                "version": __version__,
                "agent": agent,
                "oof_level": feb.oof_level,
                "has_feb": feb.has_feb,
            }
        )
    except Exception as e:
        logger.warning("webui.py: %s", e)
        return JSONResponse(
            {"status": "ok", "service": "skchat-webui", "version": __version__}
        )


@app.get("/agent/state")
async def agent_state() -> JSONResponse:
    """Return the running agent's identity, soul summary, and FEB state.

    This is the canonical "who am I and how do I feel" endpoint. The webui
    has no way to surface this without a real agent profile loader; before
    the v0.3.2 fix the page-rendered identity was hardcoded to
    ``capauth:skchat@skworld.io`` and OOF defaulted to 100% because no FEB
    selection ever ran. ``/agent/state`` is the diagnostic surface that
    proves both fixes landed.
    """
    try:
        from .agent_profile import load_agent_profile

        profile = load_agent_profile()
        return JSONResponse(profile.to_dict())
    except Exception as exc:
        logger.warning("webui.py: %s", exc)
        return JSONResponse(
            {"error": "agent_profile_load_failed", "detail": str(exc)},
            status_code=500,
        )


# Serve /voice page even when torch/silero are unavailable (voice WS won't work
# but the static HTML page will still load and attempt to connect)
if not _voice_routes_loaded:
    from fastapi.responses import FileResponse as _FileResponse

    @app.get("/voice", response_class=HTMLResponse)
    async def voice_chat_page_fallback() -> HTMLResponse:
        _static = Path(__file__).parent / "static" / "voice-chat.html"
        if _static.exists():
            return _FileResponse(_static, media_type="text/html")
        return HTMLResponse("<h1>voice-chat.html not found</h1>", status_code=404)


# ── WebSocket connection registry ─────────────────────────────────────────────

_ws_connections: set[WebSocket] = set()
_last_push_dt: Optional[datetime] = None


async def _ws_broadcast(msg_dict: dict) -> None:
    """Send a JSON payload to all connected WebSocket clients."""
    if not _ws_connections:
        return
    payload = json.dumps(msg_dict, default=str)
    dead: list[WebSocket] = []
    for ws in list(_ws_connections):
        try:
            await ws.send_text(payload)
        except Exception as e:
            logger.warning("webui.py: %s", e)
            dead.append(ws)
    for ws in dead:
        _ws_connections.discard(ws)


# ── helpers ───────────────────────────────────────────────────────────────────


def _get_identity() -> str:
    """Resolve the running agent's CapAuth identity URI.

    Order:
        1. Active SK agent profile (``SKAGENT`` / ``SKCAPSTONE_AGENT``) →
           per-agent ``identity/identity.json`` or convention
           ``capauth:{agent}@skworld.io``. This is the sovereign path —
           when the operator launches as ``SKAGENT=lumina``, the webui
           identifies as Lumina, not as the literal "skchat" service.
        2. ``identity_bridge.get_sovereign_identity()`` for legacy
           single-identity deployments.
        3. ``SKCHAT_IDENTITY`` env var (the historical hardcoded shim).
        4. ``~/.skchat/config.yml`` ``skchat.identity.uri``.
        5. ``capauth:local@skchat`` floor.
    """
    try:
        from .agent_profile import get_active_agent_name, get_agent_identity

        if get_active_agent_name() is not None:
            return get_agent_identity()
    except Exception as e:
        logger.warning("webui.py: %s", e)
        pass

    try:
        from .identity_bridge import get_sovereign_identity

        return get_sovereign_identity()
    except Exception as e:
        logger.warning("webui.py: %s", e)
        pass
    identity = os.environ.get("SKCHAT_IDENTITY")
    if identity:
        return identity
    config = _SKCHAT_HOME / "config.yml"
    if config.exists():
        try:
            import yaml

            with open(config) as f:
                cfg = yaml.safe_load(f)
            return cfg.get("skchat", {}).get("identity", {}).get("uri", "capauth:local@skchat")
        except Exception as e:
            logger.warning("webui.py: %s", e)
            pass
    return "capauth:local@skchat"


def _get_history():
    from .history import ChatHistory

    return ChatHistory()


def _skchat_home() -> Path:
    """Resolve the skchat home dir, honouring SKCHAT_HOME (tests sandbox it)."""
    return Path(os.environ.get("SKCHAT_HOME", str(Path.home() / ".skchat")))


# Transfer ids are path components served from disk — restrict to a safe charset
# (no slashes / dotdot) so a request can never escape the per-subdir base.
_TID_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_transfer_dir(transfer_id: str, sub: str) -> Optional[Path]:
    """Resolve <home>/<sub>/<transfer_id>, guarding against path traversal.

    Returns the directory only if the transfer_id is well-formed and the
    resolved path stays under the base; otherwise None.
    """
    if not _TID_RE.match(transfer_id):
        return None
    base = (_skchat_home() / sub).resolve()
    target = (base / transfer_id).resolve()
    if base not in target.parents and target != base:
        return None
    return target if target.exists() else None


def _attachment_service():
    """Build an AttachmentService bound to a real FileTransferService."""
    from .attachments import AttachmentService
    from .files import FileTransferService

    ident = _get_identity()
    fs = FileTransferService(ident)
    return AttachmentService(ident, _get_history(), fs)


def _get_transport(identity: str):
    try:
        from skcomms import SKComms

        from .transport import ChatTransport

        comm = SKComms.from_config()
        return ChatTransport(skcomms=comm, history=_get_history(), identity=identity)
    except Exception as e:
        logger.warning("webui.py: %s", e)
        return None


def _display_name(uri: str) -> str:
    if not uri:
        return ""
    try:
        from .identity_bridge import resolve_display_name

        return resolve_display_name(uri)
    except Exception as e:
        logger.warning("webui.py: %s", e)
        pass
    try:
        local = uri.split(":", 1)[1] if ":" in uri else uri
        return (local.split("@", 1)[0] if "@" in local else local).capitalize()
    except Exception as e:
        logger.warning("webui.py: %s", e)
        return uri


def _msg_css(sender: str, my_identity: str) -> str:
    if sender == my_identity:
        return "self"
    lower = sender.lower()
    if "lumina" in lower:
        return "lumina"
    if "chef" in lower:
        return "chef"
    return ""


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>SKChat</title>
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
<style>
*{box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:monospace;margin:0}
#chat{height:calc(100vh - 52px);overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:2px}
.msg .ts{color:#444;font-size:.8em;margin-right:5px}
.msg .who{font-weight:bold;margin-right:6px}
.msg .who{color:#4a9eff}
.self  .who{color:#4ade80}
.lumina .who{color:#c084fc}
.chef  .who{color:#fbbf24}
#bar{position:fixed;bottom:0;width:100%;background:#111;padding:8px 12px;display:flex;gap:8px;border-top:1px solid #1e1e1e}
select,input[type=text]{background:#1a1a1a;border:1px solid #2a2a2a;color:#e0e0e0;padding:6px 8px;font-family:monospace}
select{min-width:170px}input[type=text]{flex:1}
button{background:#1d4ed8;color:#fff;border:none;padding:6px 16px;cursor:pointer;font-family:monospace}
button:hover{background:#2563eb}
#ws-dot{position:fixed;top:5px;right:8px;font-size:.75em;color:#333}
#ws-dot.live{color:#4ade80}
.att-img{max-width:240px;border-radius:8px;display:block;margin-top:4px}
.att-file{display:inline-block;margin-top:4px;color:#7dd3fc;text-decoration:none}
.att-file:hover{text-decoration:underline}
#attach-btn{background:#374151}
#attach-btn:hover{background:#4b5563}
#upload-progress{position:fixed;bottom:46px;width:100%;background:#111;padding:4px 12px;font-size:.8em;color:#9ca3af;border-top:1px solid #1e1e1e}
#upload-progress progress{width:200px;vertical-align:middle;margin-right:8px}
</style>
</head>
<body>
<span id="ws-dot" title="WebSocket status">&#9679; ws</span>
<div id="chat" hx-get="/messages" hx-trigger="load" hx-swap="innerHTML"></div>
<form id="bar"
      hx-post="/send" hx-target="#chat" hx-swap="innerHTML"
      hx-on::after-request="this.querySelector('input[type=text]').value=''">
  <select name="recipient" id="recipient-sel">
    <option value="capauth:lumina@skworld.io">@Lumina</option>
    <option value="d4f3281e-fa92-474c-a8cd-f0a2a4c31c33">skworld-team</option>
  </select>
  <input type="text" name="content" placeholder="Message\u2026" autofocus autocomplete="off">
  <input type="file" id="file-input" multiple style="display:none">
  <button type="button" id="attach-btn" title="Attach">\U0001F4CE</button>
  <button type="submit">Send</button>
</form>
<div id="upload-progress" style="display:none"><progress id="up-bar" max="100" value="0"></progress> <span id="up-label"></span></div>
<script>
(function(){
  var fi = document.getElementById('file-input');
  document.getElementById('attach-btn').onclick = function(){ fi.click(); };
  function recipient(){ return document.getElementById('recipient-sel').value; }
  function caption(){ var c = document.querySelector('input[name=content]'); return c ? c.value : ''; }
  function uploadFiles(files){
    var prog = document.getElementById('upload-progress');
    var bar = document.getElementById('up-bar'), label = document.getElementById('up-label');
    var list = Array.prototype.slice.call(files);
    var chain = Promise.resolve();
    list.forEach(function(f){
      chain = chain.then(function(){
        return new Promise(function(res){
          prog.style.display='block'; bar.value=0; label.textContent='Uploading '+f.name+'\u2026';
          var fd = new FormData();
          fd.append('recipient', recipient());
          fd.append('caption', caption());
          fd.append('file', f);
          var xhr = new XMLHttpRequest();
          xhr.open('POST','/upload');
          xhr.upload.onprogress = function(e){ if(e.lengthComputable) bar.value = Math.round(100*e.loaded/e.total); };
          xhr.onload = function(){ res(); };
          xhr.onerror = function(){ res(); };
          xhr.send(fd);
        });
      });
    });
    chain.then(function(){
      prog.style.display='none';
      if (window.htmx) htmx.ajax('GET','/messages',{target:'#chat',swap:'innerHTML'});
    });
  }
  fi.onchange = function(){ if (fi.files.length) uploadFiles(fi.files); fi.value=''; };
  var chat = document.getElementById('chat');
  ['dragover','drop'].forEach(function(ev){ chat.addEventListener(ev, function(e){ e.preventDefault(); }); });
  chat.addEventListener('drop', function(e){ if(e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });
  document.addEventListener('paste', function(e){
    var items = (e.clipboardData && e.clipboardData.items) ? Array.prototype.slice.call(e.clipboardData.items) : [];
    var imgs = items.filter(function(i){ return i.type.indexOf('image/')===0; })
                    .map(function(i){ return i.getAsFile(); })
                    .filter(Boolean);
    if(imgs.length) uploadFiles(imgs);
  });
})();
</script>
<script>
(function(){
  var ws, rtimer;
  var dot = document.getElementById('ws-dot');
  function connect(){
    clearTimeout(rtimer);
    try { ws = new WebSocket('ws://'+location.host+'/ws/chat'); } catch(e){ return; }
    ws.onopen = function(){ dot.className='live'; };
    ws.onmessage = function(e){
      var msg;
      try { msg = JSON.parse(e.data); } catch(_){ return; }
      if(msg.type === 'new'){
        htmx.ajax('GET', '/messages', {target:'#chat', swap:'innerHTML'});
      }
    };
    ws.onclose = function(){ dot.className=''; rtimer = setTimeout(connect, 4000); };
  }
  connect();
  // Populate known groups in recipient selector
  fetch('/groups').then(function(r){ return r.json(); }).then(function(gs){
    var sel = document.getElementById('recipient-sel');
    gs.forEach(function(g){
      var o = document.createElement('option');
      o.value = g.id; o.textContent = g.name + ' (group)';
      sel.appendChild(o);
    });
  }).catch(function(){});
})();
</script>
</body>
</html>"""


# ── message rendering ─────────────────────────────────────────────────────────


def _render_messages(history, identity: str) -> str:
    try:
        msgs = history.load(limit=100)
    except Exception as e:
        logger.warning("webui.py: %s", e)
        msgs = []

    parts: list[str] = []
    for m in reversed(msgs):  # oldest-first for display
        if hasattr(m, "sender"):
            sender = m.sender
            content = m.content
            ts_raw = getattr(m, "timestamp", None)
        elif isinstance(m, dict):
            sender = m.get("sender", "?")
            content = m.get("content", "")
            ts_raw = m.get("timestamp")
        else:
            continue

        ts_str = ""
        if ts_raw:
            try:
                if isinstance(ts_raw, str):
                    ts_raw = datetime.fromisoformat(ts_raw)
                ts_str = ts_raw.strftime("%H:%M")
            except Exception as e:
                logger.warning("webui.py: %s", e)
                pass

        css = _msg_css(sender, identity)
        name = _display_name(sender)
        safe = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        att_html = ""
        for att in getattr(m, "attachments", []) or []:
            fname = att.filename.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if att.mime_type.startswith("image/") and att.thumbnail_id:
                att_html += (
                    f'<a href="/file/{att.transfer_id}" target="_blank">'
                    f'<img class="att-img" src="/file/{att.transfer_id}/thumb" '
                    f'alt="{fname}" loading="lazy"></a>'
                )
            else:
                kb = max(1, att.size // 1024)
                att_html += (
                    f'<a class="att-file" href="/file/{att.transfer_id}">'
                    f'\U0001F4C4 {fname} · {kb} KB · {att.mime_type}</a>'
                )

        parts.append(
            f'<div class="msg {css}">'
            f'<span class="ts">{ts_str}</span>'
            f'<span class="who">{name}</span>'
            f'<span class="text">{safe}</span>'
            f"{att_html}"
            f"</div>"
        )
    return "\n".join(parts)


# ── routes ────────────────────────────────────────────────────────────────────


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/voice", status_code=307)


@app.get("/legacy", response_class=HTMLResponse)
async def legacy_index() -> HTMLResponse:
    return HTMLResponse(_HTML)


@app.get("/pair/qr")
def pair_qr(sy: str = "1", ts: str = "1", https: str = "1", embed: str = "0"):
    import io
    import segno
    from skcomms import pairing
    def _on(v): return str(v).lower() not in ("0", "false", "no", "off", "")

    def _build(embed_key: bool):
        b = pairing.bundle_from_self(embed_key=embed_key)
        if not _on(sy): b.syncthing_device_id = None
        if not _on(ts): b.tailscale = None
        if not _on(https): b.https = None
        return b, pairing.to_skp_uri(b)

    bundle, uri = _build(_on(embed))
    warning = None
    try:
        # error="l" = max data capacity (a QR tops out ~2953 bytes).
        qr = segno.make(uri, error="l")
    except Exception:  # segno.encoder.DataOverflowError — key too big to embed
        bundle, uri = _build(False)            # fall back to a compact QR
        qr = segno.make(uri, error="l")
        warning = ("Public key too large to embed in a QR — using a compact "
                   "code (the peer fetches + verifies the key on accept).")
    buf = io.BytesIO(); qr.save(buf, kind="svg", scale=5)
    return {"uri": uri, "svg": buf.getvalue().decode("utf-8"),
            "fqid": bundle.fqid, "fingerprint": bundle.fingerprint,
            "embedded": bundle.pubkey is not None, "warning": warning}


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
</script>
<div id="ring-banner" style="display:none;position:fixed;top:0;left:0;right:0;
  background:#143;color:#fff;padding:12px;text-align:center;z-index:9999"></div>
<div id="peer-list" style="max-width:520px;margin:12px auto;font-family:sans-serif"></div>
<script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
async function loadPeers(){
  try{
    const r = await fetch('/call/peers'); if(!r.ok)return;
    const {peers} = await r.json();
    const el = document.getElementById('peer-list');
    if(!peers || !peers.length){ el.innerHTML = '<em>No paired peers yet.</em>'; return; }
    el.innerHTML = '<h3>Paired peers</h3>' + peers.map(p =>
      '<div style="padding:6px;border-bottom:1px solid #ccc;display:flex;'
      +'justify-content:space-between;align-items:center">'
      +'<span>'+esc(p.fqid)+'</span>'
      +'<button data-fqid="'+esc(p.fqid)+'" class="call-btn">📞 Call</button></div>'
    ).join('');
    el.querySelectorAll('.call-btn').forEach(function(btn){
      btn.addEventListener('click', function(){ callPeer(this.dataset.fqid); });
    });
  }catch(e){}
}
loadPeers();
</script>
<script>
async function callPeer(fqid){
  const r = await fetch('/call/start',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({peer:fqid})});
  if(!r.ok){alert('call failed: '+r.status);return;}
  const d = await r.json();
  location.href = '/livekit?room='+encodeURIComponent(d.room)
    +'&identity='+encodeURIComponent(d.identity)
    +'&token='+encodeURIComponent(d.token);
}
async function answerPeer(fqid){
  const r = await fetch('/call/answer',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({peer:fqid})});
  if(!r.ok){alert('answer failed: '+r.status);return;}
  const d = await r.json();
  location.href = '/livekit?room='+encodeURIComponent(d.room)
    +'&identity='+encodeURIComponent(d.identity)
    +'&token='+encodeURIComponent(d.token);
}
async function pollRing(){
  try{
    const r = await fetch('/call/incoming'); if(!r.ok)return;
    const {invites} = await r.json();
    const b = document.getElementById('ring-banner');
    if(invites && invites.length){
      const inv = invites[0];
      b.innerHTML = '📞 Incoming call from '+esc(inv.from_fqid)+' '
        +'<button class="answer-btn" data-fqid="'+esc(inv.from_fqid)+'">Accept</button>';
      b.querySelector('.answer-btn').addEventListener('click', function(){ answerPeer(this.dataset.fqid); });
      b.style.display='block';
    } else { b.style.display='none'; }
  }catch(e){}
}
setInterval(pollRing, 4000); pollRing();
</script></body></html>"""


@app.get("/pair", response_class=HTMLResponse)
async def pair_page() -> HTMLResponse:
    return HTMLResponse(_PAIR_HTML)


@app.post("/pair/accept")
async def pair_accept(payload: dict = Body(...)):
    """Accept a scanned/pasted skp:// pairing URI.

    Delegates to ``skcomms.pairing.accept_pairing`` which securely verifies the
    key fingerprint before TOFU-adding the peer; a fingerprint mismatch or an
    unresolvable key raises ``ValueError`` → mapped to HTTP 400.
    """
    from skcomms import pairing

    from .pairing_gate import gate_required, get_gate

    uri = (payload or {}).get("uri", "").strip()
    if not uri:
        raise HTTPException(status_code=400, detail="missing 'uri'")
    # When exposed publicly (Tailscale Funnel), require an operator-opened,
    # time-boxed pairing window + nonce + rate limit. Tailnet usage is unchanged.
    gate = get_gate()
    if gate_required():
        ok, reason = gate.check((payload or {}).get("nonce", ""))
        if not ok:
            code = 429 if "rate limited" in reason else 403
            raise HTTPException(status_code=code, detail=reason)
    try:
        res = pairing.accept_pairing(uri)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if gate_required():
        gate.consume()
    # peer list / a system note may change
    asyncio.create_task(_ws_broadcast({"type": "new"}))
    return res


@app.post("/pair/open")
async def pair_open():
    """Operator opens a time-boxed pairing window; returns a nonce.

    Keep this endpoint tailnet-only — do NOT expose it over Funnel. Only
    ``/pair/scan`` + ``/pair/accept`` should be public; the operator opens the
    window from the trusted side, and the remote device presents the nonce.
    """
    import os

    from .pairing_gate import get_gate

    info = get_gate().open_window()
    # Ready-to-share public scan URL (carries the gate nonce). The remote opens
    # this, scans the skp:// QR, and the page posts {uri, nonce} to /pair/accept.
    base = os.getenv("SKCHAT_FUNNEL_PUBLIC_URL", "").rstrip("/")
    if base:
        info["scan_url"] = f"{base}/pair/scan?gate={info['nonce']}"
    return info


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
 var gate=new URLSearchParams(location.search).get('gate')||'';
 fetch('/pair/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uri:uri,nonce:gate})})
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


@app.get("/messages", response_class=HTMLResponse)
async def messages() -> HTMLResponse:
    identity = _get_identity()
    history = _get_history()
    return HTMLResponse(_render_messages(history, identity))


@app.post("/send", response_class=HTMLResponse)
async def send(recipient: str = Form(...), content: str = Form(...)) -> HTMLResponse:
    if content.strip():
        identity = _get_identity()
        transport = _get_transport(identity)
        if transport:
            transport.send_and_store(recipient=recipient, content=content)
        else:
            from .models import ChatMessage

            msg = ChatMessage(sender=identity, recipient=recipient, content=content)
            _get_history().save(msg)
        # Notify WS clients so they refresh
        asyncio.create_task(_ws_broadcast({"type": "new"}))
    identity = _get_identity()
    history = _get_history()
    return HTMLResponse(_render_messages(history, identity))


@app.post("/upload")
async def upload(
    recipient: str = Form(...),
    caption: str = Form(""),
    file: UploadFile = File(...),
) -> JSONResponse:
    """Accept a multipart file, stage it, and send it as a chat attachment."""
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    home = _skchat_home()
    staged = home / "uploads" / _uuid.uuid4().hex / (file.filename or "upload.bin")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    svc = _attachment_service()
    msg = svc.send_attachment(recipient, staged, caption=caption or None)
    asyncio.create_task(_ws_broadcast({"type": "new"}))
    return JSONResponse(
        {
            "id": msg.id,
            "transfer_id": msg.attachments[0].transfer_id,
            "filename": msg.attachments[0].filename,
        }
    )


@app.get("/file/{transfer_id}")
def download_file(transfer_id: str) -> FileResponse:
    """Download the file for a completed transfer (path-traversal guarded)."""
    d = _safe_transfer_dir(transfer_id, "received") or _safe_transfer_dir(
        transfer_id, "uploads"
    )
    if d is None:
        raise HTTPException(status_code=404, detail="not found")
    files = [p for p in d.rglob("*") if p.is_file() and p.name != "thumb.webp"]
    if not files:
        raise HTTPException(status_code=404, detail="empty")
    f = files[0]
    return FileResponse(
        str(f),
        filename=f.name,
        headers={"Content-Disposition": f'attachment; filename="{f.name}"'},
    )


@app.get("/file/{transfer_id}/thumb")
def file_thumb(transfer_id: str) -> FileResponse:
    """Serve the WebP thumbnail for an image transfer, if one exists."""
    for sub in ("received", "thumbnails", "uploads"):
        d = _safe_transfer_dir(transfer_id, sub)
        if d and (d / "thumb.webp").exists():
            return FileResponse(str(d / "thumb.webp"), media_type="image/webp")
    raise HTTPException(status_code=404, detail="no thumbnail")


@app.get("/inbox")
async def inbox(limit: int = 100, since_minutes: int = 1440) -> JSONResponse:
    """Return recent messages as JSON.

    Args:
        limit: Max messages to return.
        since_minutes: Look-back window (0 = all).
    """
    history = _get_history()
    since: Optional[datetime] = (
        datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        if since_minutes > 0
        else None
    )
    msgs = history.load(since=since, limit=limit)
    return JSONResponse(
        [
            {
                "id": m.id,
                "sender": m.sender,
                "recipient": m.recipient,
                "content": m.content,
                "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                "delivery_status": m.delivery_status.value,
                "thread_id": m.thread_id,
            }
            for m in msgs
        ]
    )


@app.get("/groups")
async def groups() -> JSONResponse:
    """Return known groups loaded from ~/.skchat/groups/*.json.

    Each member is enriched with peer-registry data (entity_type, fingerprint,
    soul-derived display name) so the UI can distinguish humans from agents
    without bare-URI rendering.
    """
    from .group import GroupChat
    from .peer_discovery import PeerDiscovery

    discovery = PeerDiscovery()
    groups_dir = _SKCHAT_HOME / "groups"
    result: list[dict] = []
    if not groups_dir.exists():
        return JSONResponse(result)

    for f in sorted(groups_dir.glob("*.json")):
        try:
            grp = GroupChat.model_validate_json(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("webui.py: %s", e)
            continue

        members_out = []
        for m in grp.members:
            peer = discovery.get_peer(m.identity_uri) or {}
            entity_type = peer.get("entity_type") or m.participant_type.value
            display_name = (
                m.display_name
                or peer.get("name")
                or m.identity_uri.split(":")[-1].split("@")[0]
            )
            members_out.append(
                {
                    "uri": m.identity_uri,
                    "role": m.role.value,
                    "participant_type": m.participant_type.value,
                    "display_name": display_name,
                    "entity_type": entity_type,
                    "fingerprint": peer.get("fingerprint", ""),
                    "trust_level": peer.get("trust_level", "unknown"),
                }
            )

        result.append(
            {
                "id": grp.id,
                "name": grp.name,
                "description": grp.description,
                "member_count": grp.member_count,
                "members": members_out,
                "message_count": grp.message_count,
                "created_at": grp.created_at.isoformat(),
                "updated_at": grp.updated_at.isoformat(),
            }
        )
    return JSONResponse(result)


# ── WebSocket real-time push ───────────────────────────────────────────────────


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """Real-time push channel. Clients receive {type: 'new'} when the history
    has new messages, and should re-fetch /messages in response."""
    await websocket.accept()
    _ws_connections.add(websocket)
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=20)
                if data == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "heartbeat"}))
            except WebSocketDisconnect:
                break
    except Exception as e:
        logger.warning("webui.py: %s", e)
        pass
    finally:
        _ws_connections.discard(websocket)


# ── Background poller ─────────────────────────────────────────────────────────


async def _background_message_poller() -> None:
    """Poll JSONL history every 3 s; push {type:'new'} to WS clients when
    messages arrive after startup (e.g. daemon wrote them)."""
    global _last_push_dt
    _last_push_dt = datetime.now(timezone.utc)
    await asyncio.sleep(2)  # let app fully start
    while True:
        await asyncio.sleep(3)
        if not _ws_connections:
            continue
        try:
            history = _get_history()
            new_msgs = history.load(since=_last_push_dt, limit=20)
            if new_msgs:
                # load() is newest-first; update cutoff past the newest timestamp
                newest = new_msgs[0].timestamp
                if newest is not None:
                    if newest.tzinfo is None:
                        newest = newest.replace(tzinfo=timezone.utc)
                    _last_push_dt = newest + timedelta(microseconds=1)
                await _ws_broadcast({"type": "new", "count": len(new_msgs)})
        except Exception as e:
            logger.warning("webui.py: %s", e)
            pass


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_background_message_poller())


# ── entry point ───────────────────────────────────────────────────────────────


def run(port: int = 8765, open_browser: bool = True, host: str = "") -> None:
    """Start the SKChat Web UI server (blocking)."""
    import uvicorn

    if not host:
        host = os.environ.get("SKCHAT_HOST", "127.0.0.1")
    if open_browser:
        webbrowser.open(f"http://localhost:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
