"""SKChat Web UI — minimal FastAPI + HTMX chat interface.

Usage:  skchat webui [--port 8765] [--no-browser]
"""

from __future__ import annotations

import asyncio
import json
import os
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="SKChat Web UI")
_SKCHAT_HOME = Path("~/.skchat").expanduser()

# Register voice streaming routes (/ws/voice, /voice)
try:
    from .voice_stream import register_voice_routes as _register_voice_routes

    _register_voice_routes(app)
    _voice_routes_loaded = True
except ImportError:
    # torch/silero not available — use lightweight handler (client-side VAD)
    try:
        from .voice_ws_lite import register_voice_routes_lite as _register_voice_routes_lite

        _register_voice_routes_lite(app)
        _voice_routes_loaded = True
    except ImportError:
        _voice_routes_loaded = False


@app.get("/health")
async def health() -> JSONResponse:
    """Health check endpoint for container orchestration."""
    return JSONResponse({"status": "ok", "service": "skchat-webui", "version": "0.1.1"})


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
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_connections.discard(ws)


# ── helpers ───────────────────────────────────────────────────────────────────


def _get_identity() -> str:
    try:
        from .identity_bridge import get_sovereign_identity

        return get_sovereign_identity()
    except Exception:
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
        except Exception:
            pass
    return "capauth:local@skchat"


def _get_history():
    from .history import ChatHistory

    return ChatHistory()


def _get_transport(identity: str):
    try:
        from skcomm import SKComm

        from .transport import ChatTransport

        comm = SKComm.from_config()
        return ChatTransport(skcomm=comm, history=_get_history(), identity=identity)
    except Exception:
        return None


def _display_name(uri: str) -> str:
    if not uri:
        return ""
    try:
        from .identity_bridge import resolve_display_name

        return resolve_display_name(uri)
    except Exception:
        pass
    try:
        local = uri.split(":", 1)[1] if ":" in uri else uri
        return (local.split("@", 1)[0] if "@" in local else local).capitalize()
    except Exception:
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
  <button type="submit">Send</button>
</form>
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
    except Exception:
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
            except Exception:
                pass

        css = _msg_css(sender, identity)
        name = _display_name(sender)
        safe = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(
            f'<div class="msg {css}">'
            f'<span class="ts">{ts_str}</span>'
            f'<span class="who">{name}</span>'
            f'<span class="text">{safe}</span>'
            f"</div>"
        )
    return "\n".join(parts)


# ── routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_HTML)


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
    """Return known groups loaded from ~/.skchat/groups/*.json."""
    groups_dir = _SKCHAT_HOME / "groups"
    result: list[dict] = []
    if groups_dir.exists():
        for f in sorted(groups_dir.glob("*.json")):
            try:
                from .group import GroupChat

                grp = GroupChat.model_validate_json(f.read_text(encoding="utf-8"))
                result.append(
                    {
                        "id": grp.id,
                        "name": grp.name,
                        "description": grp.description,
                        "member_count": grp.member_count,
                        "members": [
                            {
                                "uri": m.identity_uri,
                                "role": m.role.value,
                                "display_name": m.display_name,
                            }
                            for m in grp.members
                        ],
                        "message_count": grp.message_count,
                        "created_at": grp.created_at.isoformat(),
                        "updated_at": grp.updated_at.isoformat(),
                    }
                )
            except Exception:
                pass
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
    except Exception:
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
        except Exception:
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
