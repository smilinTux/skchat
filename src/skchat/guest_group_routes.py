"""FastAPI routes for GUEST GROUP access (one-link, group-scoped, untrusted).

Two route families, both gated by ``SKCHAT_GUEST_LINKS_ENABLED``:

* **Operator** (capauth/operator-gated, reuses ``guest._require_operator``):
  mint / list / revoke a room-scoped invite for a group.
* **Guest** (guest-session-token-gated): join, then the FULL in-room kit for the
  ONE bound group — read history, send signed messages, react, upload+download
  files, and get a LiveKit guest call token (publish A/V/screen). EVERYTHING is
  pinned to the ``group_id`` carried in the guest's session token; a request for
  any other group/conversation/file → 403. There is NO guest endpoint for
  invite/create/admin/peer-list/agent-tools — that surface simply does not exist.

When the flag is OFF: operator routes 404, guest routes 403 (no oracle).
"""

from __future__ import annotations

import logging
import os
import time
import uuid as _uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from skchat import guest_groups as GG

logger = logging.getLogger("skchat.guest_group_routes")

router = APIRouter(prefix="/api/v1")

# Max guest upload (50 MiB — smaller than the operator cap; guests are untrusted).
MAX_GUEST_UPLOAD = 50 * 1024 * 1024

# Transfer-id charset guard (path component served from disk).
import re as _re  # noqa: E402

_TID_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _skchat_home() -> Path:
    return Path(os.environ.get("SKCHAT_HOME", str(Path.home() / ".skchat")))


def _require_flag_operator() -> None:
    """Operator routes 404 when the feature is off (don't reveal they exist)."""
    if not GG.guest_links_enabled():
        raise HTTPException(404, "not found")


def _require_flag_guest() -> None:
    """Guest routes 403 when the feature is off."""
    if not GG.guest_links_enabled():
        raise HTTPException(403, "guest links disabled")


def _history():
    from skchat import daemon_proxy

    return daemon_proxy._get_history()


def _guest_session(request: Request) -> GG.GuestSession:
    """Extract + verify the guest session token from the request, or 403.

    Accepted as ``Authorization: Bearer <jwt>`` or ``X-Guest-Token: <jwt>``.
    The returned session pins the request to exactly one group_id.
    """
    headers = request.headers
    tok = (headers.get("x-guest-token") or "").strip()
    if not tok:
        auth = (headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    if not tok:
        raise HTTPException(403, "guest session token required")
    try:
        return GG.verify_guest_session(tok)
    except GG.SessionInvalid as exc:
        logger.info("guest session rejected: %s", exc)
        raise HTTPException(403, "invalid or expired guest session") from exc


def _bound_group(session: GG.GuestSession):
    """Load the group the session is bound to (404 if it vanished)."""
    from skchat import daemon_proxy_groups as G

    group = G.load_group(session.group_id)
    if group is None:
        raise HTTPException(404, "group not found")
    return group


def _assert_same_group(session: GG.GuestSession, requested_group_id: str) -> None:
    """403 unless ``requested_group_id`` matches the token's bound group.

    The single chokepoint for one-room isolation: any guest request that names a
    group id (path/body) is checked against the token's group_id before any work.
    """
    if (requested_group_id or "").strip() and requested_group_id != session.group_id:
        raise HTTPException(403, "guest is scoped to a single group")


# --------------------------------------------------------------------------- #
# Operator: invite mint / list / revoke
# --------------------------------------------------------------------------- #
@router.post("/groups/{group_id}/invite")
async def operator_create_invite(group_id: str, request: Request, mode: str = "group"):
    """Operator-only: mint a room-scoped, signed invite for ``group_id``.

    Body (all optional): ``{ttl?, single_use?}``. Query ``?mode=dm|group``
    (default ``group``): ``mode=dm`` mints a NEW 2-seat DM guest group
    (``metadata.mode="dm"``, seat 1 = operator) and invites into it — the path
    ``group_id`` is unused in that case; ``mode=group`` is the unchanged
    behaviour (invite into the existing ``group_id``). Returns ``{token,
    join_url, ...}``. Operator-gated (tailnet/loopback or
    ``SKCHAT_GUEST_OPERATOR_TOKEN``); 404 when the feature flag is off.
    """
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)

    try:
        body = await request.json()
    except Exception:
        body = {}
    ttl_raw = body.get("ttl")
    ttl = None
    if ttl_raw is not None:
        try:
            ttl = int(ttl_raw)
        except (TypeError, ValueError):
            ttl = None

    if (mode or "group").strip().lower() == "dm":
        # A 1:1 DM invite mints its OWN 2-seat guest group; the path group_id is
        # not used. DMs default single-use (override via body).
        try:
            result = GG.create_dm_invite(
                single_use=bool(body.get("single_use", True)), ttl=ttl
            )
        except RuntimeError as exc:  # secret unset
            raise HTTPException(503, str(exc)) from exc
        logger.info("guest-group DM invite minted (jti=%s gid=%s)", result["jti"], result["group_id"])
        return JSONResponse(result)

    from skchat import daemon_proxy_groups as G

    if G.load_group(group_id) is None:
        raise HTTPException(404, "group not found")

    single_use = bool(body.get("single_use", False))
    try:
        result = GG.create_group_invite(group_id, ttl=ttl, single_use=single_use)
    except RuntimeError as exc:  # secret unset
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    logger.info("guest-group invite minted for %s (jti=%s)", group_id, result["jti"])
    return JSONResponse(result)


@router.delete("/groups/{group_id}/invite/{token}")
async def operator_revoke_invite(group_id: str, token: str, request: Request):
    """Operator-only: revoke an invite by its token (jti extracted, no verify)."""
    _require_flag_operator()
    from skchat.guest import _require_operator, revoke_invite

    _require_operator(request)
    jti = GG.jti_of(token)
    if not jti:
        raise HTTPException(400, "could not parse token")
    revoke_invite(jti)
    logger.info("guest-group invite revoked: group=%s jti=%s", group_id, jti)
    return JSONResponse({"ok": True, "revoked_jti": jti, "group_id": group_id})


@router.get("/guest/invite/{token}")
async def guest_invite_preview(token: str):
    """Public-of-tailnet preview of an invite (group name) for the landing page.

    Does NOT consume a single-use invite (peek only). 403 when the flag is off;
    a bad/expired/revoked token → ``{valid:false}`` (generic, no oracle).
    """
    _require_flag_guest()
    try:
        info = GG.verify_group_invite(token, burn_single_use=False)
    except GG.InviteInvalid:
        return JSONResponse({"valid": False})

    from skchat import daemon_proxy_groups as G

    group = G.load_group(info["group_id"])
    if group is None:
        return JSONResponse({"valid": False})
    return JSONResponse(
        {
            "valid": True,
            "group_id": group.id,
            "group_name": group.name,
            "expires_at": info["exp"],
        }
    )


# --------------------------------------------------------------------------- #
# Guest: join (create/lookup untrusted member → session token + call token)
# --------------------------------------------------------------------------- #
@router.post("/guest/join")
async def guest_join(request: Request):
    """Validate an invite, add the guest as an untrusted member, return tokens.

    Body: ``{invite_token, display_name, guest_pubkey}``. Returns a guest session
    token scoped to ONLY the invite's group + a LiveKit guest call token + the
    group bootstrap (id/name + initial history). The invite's single-use claim is
    burned here.
    """
    _require_flag_guest()
    try:
        body = await request.json()
    except Exception:
        body = {}
    invite_token = (body.get("invite_token") or "").strip()
    display_name = (body.get("display_name") or "").strip()
    guest_pubkey = (body.get("guest_pubkey") or "").strip()
    if not invite_token:
        raise HTTPException(400, "invite_token is required")
    if not display_name:
        raise HTTPException(400, "display_name is required")

    try:
        info = GG.verify_group_invite(invite_token, burn_single_use=True)
    except GG.InviteInvalid as exc:
        logger.info("guest-group join rejected: %s", exc)
        raise HTTPException(401, "invalid or expired invite") from exc

    group_id = info["group_id"]
    from skchat import daemon_proxy_groups as G

    group = G.load_group(group_id)
    if group is None:
        raise HTTPException(404, "group not found")

    display = GG.enforce_display_name(display_name)
    guest_id = GG.guest_identity(display_name, guest_pubkey)
    fp = GG.pubkey_fingerprint(guest_pubkey)

    # Mode-A DM: a 1:1 is a 2-seat guest group (seat 1 = operator). A NEW guest
    # that would take a third seat is refused (the DM is full). A returning guest
    # (same identity) is idempotent and always allowed.
    if (
        group.metadata.get("mode") == "dm"
        and group.get_member(guest_id) is None
        and group.member_count >= GG.DM_SEAT_CAP
    ):
        logger.info("dm join rejected: %s full (%d seats)", group_id, group.member_count)
        raise HTTPException(403, "direct message is full")

    GG.add_untrusted_guest_member(group, guest_id, display)
    G.save_group(group)

    session = GG.mint_guest_session(
        group_id=group_id, guest_id=guest_id, name=display, fp=fp
    )

    # LiveKit guest call token (publish A/V + screen + subscribe, never admin) —
    # reuse the group call room derivation so guests + members share one room.
    call = _mint_guest_call_token(group_id, guest_id, display, request)

    bootstrap = _guest_messages(group_id, limit=200, guest_id=guest_id)
    return JSONResponse(
        {
            "ok": True,
            "session_token": session,
            "guest_id": guest_id,
            "display_name": display,
            "fingerprint": fp,
            "trust": "untrusted",
            "group": {"id": group.id, "name": group.name},
            "call": call,
            "messages": bootstrap,
        }
    )


def _mint_guest_call_token(group_id: str, guest_id: str, display: str, request: Request) -> dict:
    """Build a guest LiveKit token for the group's deterministic call room.

    Publish audio/video/**screen** + subscribe + data, never room_admin. The
    grant is sourced through the conf GUEST factory (in ``guest.build_livekit_
    token`` via the GuestToken dataclass) so the admin denial is structural.
    Degrades to ``{available:false}`` when LiveKit creds are absent.
    """
    from skchat import daemon_proxy_groupcall as GC
    from skchat.livekit_routes import _have_creds

    room = GC.derive_group_room(group_id)
    if not _have_creds():
        return {"available": False, "room": room}

    from skchat.guest import GuestToken, build_livekit_token

    key = os.getenv("SKCHAT_LIVEKIT_API_KEY", "")
    secret = os.getenv("SKCHAT_LIVEKIT_API_SECRET", "")
    gt = GuestToken(
        jti=GG.pubkey_fingerprint(guest_id),
        room=room,
        identity=guest_id,
        display=display,
        exp=time.time() + GG.GUEST_CALL_TOKEN_TTL,
    )
    try:
        token = build_livekit_token(
            gt,
            livekit_api_key=key,
            livekit_api_secret=secret,
            allow_screenshare=True,  # guests get screenshare in-room
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never 500 the join
        logger.warning("guest call token mint failed: %s", exc)
        return {"available": False, "room": room}

    try:
        from skchat.livekit_routes import public_aware_livekit_url

        lk_url = public_aware_livekit_url(request)
    except Exception:
        lk_url = os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")
    return {
        "available": True,
        "room": room,
        "token": token,
        "lk_url": lk_url,
        "identity": guest_id,
        "ttl_seconds": GG.GUEST_CALL_TOKEN_TTL,
    }


# --------------------------------------------------------------------------- #
# Guest: read the bound group thread
# --------------------------------------------------------------------------- #
def _msg_ts_epoch(m) -> float:
    """Best-effort epoch seconds for a message timestamp (datetime/number/iso)."""
    ts = getattr(m, "timestamp", None)
    if isinstance(ts, (int, float)):
        return float(ts)
    if hasattr(ts, "timestamp"):
        try:
            return float(ts.timestamp())
        except Exception:
            return 0.0
    if isinstance(ts, str) and ts:
        from datetime import datetime

        try:
            return datetime.fromisoformat(ts).timestamp()
        except Exception:
            return 0.0
    return 0.0


def _dm_epoch_fence(group_id: str, guest_id: str):
    """Return the epoch-fence cutoff (``added_at``) for a DM guest, else None.

    A ``mode="dm"`` guest sees no group history from before it joined (SimpleX
    "no pre-epoch history"). Non-dm groups are NOT fenced — existing group-invite
    history behaviour is unchanged.
    """
    if not guest_id:
        return None
    from skchat import daemon_proxy_groups as G

    group = G.load_group(group_id)
    if group is None or group.metadata.get("mode") != "dm":
        return None
    entry = (group.metadata.get("guests") or {}).get(guest_id)
    if not entry:
        return None
    added = entry.get("added_at")
    try:
        return float(added) if added is not None else None
    except (TypeError, ValueError):
        return None


def _guest_messages(group_id: str, limit: int = 200, *, guest_id: str = "") -> list[dict]:
    """Load the bound group's thread in the app message contract (guest view).

    Reuses ``daemon_proxy._group_msg_to_app`` so the guest UI gets the identical
    shape members get, then decorates each message with the guest-trust markers.
    For a ``mode="dm"`` guest, an epoch fence drops any message older than the
    guest's ``added_at`` (no pre-join DM history).
    """
    from skchat import daemon_proxy
    from skchat import daemon_proxy_groups as G

    hist = _history()
    rows = G.group_thread_messages(hist, group_id, limit=limit)
    rows.sort(key=lambda x: getattr(x, "timestamp", ""))
    fence = _dm_epoch_fence(group_id, guest_id)
    out = []
    for m in rows:
        if fence is not None and _msg_ts_epoch(m) < fence:
            continue
        d = daemon_proxy._group_msg_to_app(m, group_id=group_id)
        meta = getattr(m, "metadata", {}) or {}
        if meta.get("guest"):
            d["is_guest"] = True
            d["trust"] = "untrusted"
            d["signature_present"] = bool(meta.get("guest_sig"))
        atts = getattr(m, "attachments", None) or []
        if atts:
            d["attachments"] = [a.model_dump() for a in atts]
        out.append(d)
    return out


@router.get("/guest/conversation")
async def guest_conversation(request: Request):
    """Return the bound group's thread (token-scoped). No group id is accepted —
    it is derived from the session token, so a guest can only read their room."""
    _require_flag_guest()
    session = _guest_session(request)
    _bound_group(session)  # 404 if the group vanished
    return JSONResponse(
        {
            "group_id": session.group_id,
            "messages": _guest_messages(session.group_id, guest_id=session.guest_id),
        }
    )


# --------------------------------------------------------------------------- #
# Guest: send a signed message
# --------------------------------------------------------------------------- #
@router.post("/guest/send")
async def guest_send(request: Request):
    """Post a signed guest message into the bound group.

    Body: ``{body|content, reply_to_id?, ts?, signature?, group_id?}``. If a
    ``group_id`` is supplied it MUST equal the token's group (else 403). The
    signature (detached ECDSA over the canonical ``{group_id, body, ts}``) is
    recorded as advisory metadata — it proves same-browser continuity, not
    capauth identity.
    """
    _require_flag_guest()
    session = _guest_session(request)
    group = _bound_group(session)

    try:
        body = await request.json()
    except Exception:
        body = {}
    _assert_same_group(session, (body.get("group_id") or "").strip())

    content = (body.get("body") or body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "empty message")
    reply_to_id = body.get("reply_to_id") or None
    ts = body.get("ts") or int(time.time())
    signature = (body.get("signature") or "").strip()

    from skchat import daemon_proxy
    from skchat import daemon_proxy_groups as G
    from skchat.models import ChatMessage

    hist = _history()
    # Build the message ourselves so we can stamp the guest-trust metadata before
    # the fan-out copies are derived (fan_out_send copies metadata per member).
    group_msg = ChatMessage(
        sender=session.guest_id,
        recipient=f"group:{group.id}",
        content=content,
        thread_id=group.id,
        reply_to_id=reply_to_id,
        metadata={
            "group_id": group.id,
            "group_name": group.name,
            "key_version": group.key_version,
            "guest": True,
            "trust": "untrusted",
            "guest_sig": signature or None,
            "guest_sig_ts": str(ts),
            "guest_fp": session.fp,
        },
    )
    hist.save(group_msg)
    # Per-member copies (so each member's 1:1-style inbox sees it).
    from skchat.group import MemberRole  # noqa: F401

    for member in group.members:
        if member.identity_uri == session.guest_id:
            continue
        try:
            hist.save(
                ChatMessage(
                    sender=session.guest_id,
                    recipient=member.identity_uri,
                    content=content,
                    thread_id=group.id,
                    reply_to_id=reply_to_id,
                    metadata=dict(group_msg.metadata),
                )
            )
        except Exception as exc:
            logger.warning("guest_send member copy for %s failed: %s", member.identity_uri, exc)

    group.touch()
    group.metadata["last_message"] = content
    group.metadata["last_message_time"] = group_msg.timestamp.isoformat()
    G.save_group(group)

    # Nudge web clients (members) to refresh.
    try:
        from skchat import webui as _webui

        await _webui._ws_broadcast({"type": "new"})
    except Exception:
        logger.debug("ws broadcast unavailable", exc_info=True)

    d = daemon_proxy._group_msg_to_app(group_msg, group_id=group.id)
    d["is_guest"] = True
    d["trust"] = "untrusted"
    d["signature_present"] = bool(signature)
    return JSONResponse({"ok": True, "id": group_msg.id, "message": d})


# --------------------------------------------------------------------------- #
# Guest: react
# --------------------------------------------------------------------------- #
@router.post("/guest/react")
async def guest_react(request: Request):
    """Add/remove an emoji reaction on a message in the bound group.

    Body: ``{message_id, emoji, op:"add"|"remove"}``. The reactor is the guest's
    own identity. The message must belong to the bound group (else 403).
    """
    _require_flag_guest()
    session = _guest_session(request)
    _bound_group(session)

    try:
        body = await request.json()
    except Exception:
        body = {}
    message_id = (body.get("message_id") or "").strip()
    emoji = (body.get("emoji") or "").strip()
    op = (body.get("op") or "add").strip().lower()
    if not message_id or not emoji:
        raise HTTPException(400, "message_id and emoji are required")
    if op not in ("add", "remove"):
        raise HTTPException(400, "op must be 'add' or 'remove'")

    hist = _history()
    # Verify the target message is part of THIS group's thread before mutating
    # it — a guest must not be able to react to a message in another room even
    # with a guessed id.
    from skchat import daemon_proxy_groups as G

    thread_ids = {getattr(m, "id", None) for m in G.group_thread_messages(hist, session.group_id, limit=2000)}
    if message_id not in thread_ids:
        raise HTTPException(403, "message is not in your room")

    msg = hist.set_reaction(message_id, emoji, session.guest_id) if op == "add" else \
        hist.clear_reaction(message_id, emoji, session.guest_id)
    if msg is None:
        raise HTTPException(404, "message not found")
    from skchat import daemon_proxy

    return JSONResponse(
        {"ok": True, "message": daemon_proxy._group_msg_to_app(msg, group_id=session.group_id)}
    )


# --------------------------------------------------------------------------- #
# Guest: file upload (into the bound group only)
# --------------------------------------------------------------------------- #
@router.post("/guest/file")
async def guest_file_upload(
    request: Request,
    file: UploadFile = File(...),
    caption: str = Form(""),
    group_id: str = Form(""),
):
    """Upload a file into the bound group as a chat attachment.

    Stages the bytes under ``<home>/uploads/<tid>/<filename>`` (so the shared
    ``/file/{tid}`` download works), records the transfer→group mapping (for
    guest-download isolation), and fans a FileRef message into the group thread.
    """
    _require_flag_guest()
    session = _guest_session(request)
    group = _bound_group(session)
    _assert_same_group(session, group_id)

    data = await file.read()
    if len(data) > MAX_GUEST_UPLOAD:
        raise HTTPException(413, "file too large")

    import hashlib

    tid = _uuid.uuid4().hex
    filename = file.filename or "upload.bin"
    staged = _skchat_home() / "uploads" / tid / filename
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    mime = file.content_type or "application/octet-stream"

    # Record the transfer→group binding so a guest download is group-scoped.
    GG.record_group_transfer(tid, group.id)

    from skchat import daemon_proxy
    from skchat import daemon_proxy_groups as G
    from skchat.models import ChatMessage, FileRef

    fref = FileRef(
        transfer_id=tid,
        filename=filename,
        size=len(data),
        mime_type=mime,
        sha256=sha,
        thumbnail_id=None,
        direction="sent",
    )
    group_msg = ChatMessage(
        sender=session.guest_id,
        recipient=f"group:{group.id}",
        content=caption or filename,
        thread_id=group.id,
        attachments=[fref],
        metadata={
            "group_id": group.id,
            "group_name": group.name,
            "key_version": group.key_version,
            "guest": True,
            "trust": "untrusted",
            "guest_fp": session.fp,
        },
    )
    hist = _history()
    hist.save(group_msg)
    for member in group.members:
        if member.identity_uri == session.guest_id:
            continue
        try:
            hist.save(
                ChatMessage(
                    sender=session.guest_id,
                    recipient=member.identity_uri,
                    content=caption or filename,
                    thread_id=group.id,
                    attachments=[fref],
                    metadata=dict(group_msg.metadata),
                )
            )
        except Exception as exc:
            logger.warning("guest_file member copy for %s failed: %s", member.identity_uri, exc)
    group.touch()
    group.metadata["last_message"] = caption or filename
    group.metadata["last_message_time"] = group_msg.timestamp.isoformat()
    G.save_group(group)

    try:
        from skchat import webui as _webui

        await _webui._ws_broadcast({"type": "new"})
    except Exception:
        logger.debug("ws broadcast unavailable", exc_info=True)

    d = daemon_proxy._group_msg_to_app(group_msg, group_id=group.id)
    d["is_guest"] = True
    d["trust"] = "untrusted"
    # The app serializer is text-first; surface attachments explicitly so the
    # guest UI renders the file bubble + download link.
    d["attachments"] = [fref.model_dump()]
    return JSONResponse(
        {"ok": True, "id": group_msg.id, "transfer_id": tid, "filename": filename, "message": d}
    )


@router.get("/guest/file/{transfer_id}")
async def guest_file_download(transfer_id: str, request: Request):
    """Download a file — ONLY if it belongs to the guest's bound group.

    The transfer→group allowlist is the gate: a guest can never pull a file from
    any other conversation, even with a valid transfer id from elsewhere.
    """
    _require_flag_guest()
    session = _guest_session(request)
    if not _TID_RE.match(transfer_id):
        raise HTTPException(400, "bad transfer id")
    owner_group = GG.transfer_group(transfer_id)
    if owner_group != session.group_id:
        raise HTTPException(403, "file is not in your room")

    # Serve from the staged uploads dir (guest uploads) or received dir.
    for sub in ("uploads", "received"):
        base = (_skchat_home() / sub).resolve()
        target = (base / transfer_id).resolve()
        if base not in target.parents or not target.exists():
            continue
        files = [p for p in target.rglob("*") if p.is_file() and p.name != "thumb.webp"]
        if files:
            f = files[0]
            return FileResponse(
                str(f),
                filename=f.name,
                headers={"Content-Disposition": f'attachment; filename="{f.name}"'},
            )
    raise HTTPException(404, "not found")


# --------------------------------------------------------------------------- #
# Guest: (re)mint a LiveKit call token for the bound group
# --------------------------------------------------------------------------- #
@router.post("/guest/call")
async def guest_call(request: Request):
    """Mint a fresh LiveKit guest call token for the bound group's room.

    Body (optional): ``{group_id?}`` (must match the token's group if present).
    Returns the same shape as the join response's ``call`` block: publish
    audio/video/**screen** + subscribe, never room_admin.
    """
    _require_flag_guest()
    session = _guest_session(request)
    group = _bound_group(session)
    try:
        body = await request.json()
    except Exception:
        body = {}
    _assert_same_group(session, (body.get("group_id") or "").strip())
    call = _mint_guest_call_token(group.id, session.guest_id, session.name, request)
    if not call.get("available"):
        raise HTTPException(503, "livekit not configured")
    return JSONResponse(call)


def register_guest_group_routes(app) -> None:
    """Register the guest-group router on the FastAPI app (called from webui.py)."""
    app.include_router(router)
