"""FastAPI routes for GUEST GROUP access (one-link, group-scoped, untrusted).

Two route families, both gated by ``SKCHAT_GUEST_LINKS_ENABLED``:

* **Operator** (capauth/operator-gated, reuses ``guest._require_operator``):
  mint / list / revoke a room-scoped invite for a group.
* **Guest** (guest-session-token-gated): join, then the FULL in-room kit for the
  ONE bound group - read history, send signed messages, react, upload+download
  files, and get a LiveKit guest call token (publish A/V/screen). EVERYTHING is
  pinned to the ``group_id`` carried in the guest's session token; a request for
  any other group/conversation/file → 403. There is NO guest endpoint for
  invite/create/admin/peer-list/agent-tools - that surface simply does not exist.

When the flag is OFF: operator routes 404, guest routes 403 (no oracle).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid as _uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from skchat import guest_groups as GG

logger = logging.getLogger("skchat.guest_group_routes")

router = APIRouter(prefix="/api/v1")

# Max guest upload (50 MiB - smaller than the operator cap; guests are untrusted).
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
    The returned session pins the request to exactly one group_id. This is the
    S3 enforcement chokepoint: every guest route funnels through here, so the
    ``dm_contacts`` revoked/expired check runs on every request, not just join.
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
        session = GG.verify_guest_session(tok)
    except GG.SessionInvalid as exc:
        logger.info("guest session rejected: %s", exc)
        raise HTTPException(403, "invalid or expired guest session") from exc
    _enforce_dm_contact_status(session)
    return session


def _enforce_dm_contact_status(session: GG.GuestSession) -> None:
    """S3 chokepoint: a BOUND dm guest whose contact is revoked/expired gets a
    clear, distinguishable 403 on every route - never a generic failure.

    Guests with no ``dm_contacts`` row (classic non-dm guest-group members) and
    a contact row bound to some OTHER group (defensive - the fp collided with
    an unrelated dm elsewhere) are unaffected.
    """
    contact = GG.get_dm_contact(session.fp)
    if contact is None or contact.get("group_id") != session.group_id:
        return
    if contact.get("status") == "revoked":
        raise HTTPException(403, detail={"reason": "contact_revoked"})
    expires_at = contact.get("contact_expires_at")
    if expires_at is not None and float(expires_at) <= time.time():
        raise HTTPException(403, detail={"reason": "contact_expired"})


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
    (``metadata.mode="dm"``, seat 1 = operator) and invites into it - the path
    ``group_id`` is unused in that case; ``mode=group`` is the unchanged
    behaviour (invite into the existing ``group_id``). Returns ``{token,
    join_url, ...}``. Operator-gated (tailnet/loopback or
    ``SKCHAT_GUEST_OPERATOR_TOKEN``); 404 when the feature flag is off.

    ``mode=dm`` additionally accepts: ``reusable?`` (mints the operator's
    standing my-DM-link - never single-use, carries a ``dm_reuse`` claim) and
    ``alias?``/``contact_ttl?`` (pre-set nickname + contact-expiry TTL for the
    invite, stored server-side keyed by ``jti`` - never in the JWT payload or
    any response).
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
        # not used. DMs default single-use (override via body); ``reusable`` mints
        # the operator's standing my-DM-link instead (never single-use).
        reusable = bool(body.get("reusable", False))
        single_use = False if reusable else bool(body.get("single_use", True))
        try:
            result = GG.create_dm_invite(single_use=single_use, ttl=ttl, reusable=reusable)
        except RuntimeError as exc:  # secret unset
            raise HTTPException(503, str(exc)) from exc

        # Pre-set alias + contact-expiry TTL: sidecar-only, keyed by jti. NEVER
        # placed in the JWT payload or echoed back in this (or any /guest/*)
        # response - see the SPEC CORRECTION note on the sidecar functions.
        alias_raw = body.get("alias")
        alias = (str(alias_raw).strip() or None) if alias_raw not in (None, "") else None
        contact_ttl_raw = body.get("contact_ttl")
        contact_ttl = None
        if contact_ttl_raw is not None:
            try:
                contact_ttl = int(contact_ttl_raw)
            except (TypeError, ValueError):
                contact_ttl = None
        if alias is not None or contact_ttl is not None:
            GG.store_dm_invite_meta(result["jti"], alias=alias, contact_ttl=contact_ttl)

        logger.info(
            "guest-group DM invite minted (jti=%s gid=%s reusable=%s)",
            result["jti"],
            result["group_id"],
            reusable,
        )
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


def _operator_signed_prekey():
    """The operator's current signed hybrid prekey (hybrid_public_hex), or None.

    Fail-closed (§5): on any error the guest receives no prekey and must abort the
    PQ handshake rather than silently fall back to a classical join.
    """
    try:
        from skchat import pq_prekeys as _pq

        return (_pq.agent_bundle().get("hybrid_public_hex") or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.info("guest preview: operator signed prekey unavailable: %s", exc)
        return None


def _dm_operator_display_name(group) -> str:
    """The operator's display name for a ``mode=dm`` invite landing page.

    Seat 1 (the group creator) is the admin; at mint time it is the only
    member. Falls back to the first member if no admin is recorded.
    """
    for admin_uri in group.admin_uris:
        member = group.get_member(admin_uri)
        if member is not None:
            return member.display_name
    return group.members[0].display_name if group.members else ""


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
    resp = {
        "valid": True,
        "group_id": group.id,
        "group_name": group.name,
        "expires_at": info["exp"],
    }
    if group.metadata.get("mode") == "dm":
        resp["mode"] = "dm"
        resp["operator_name"] = _dm_operator_display_name(group)
    # Phase 1: surface the operator-signed material so the joiner can verify the
    # operator signature (under the FULL inline pubkey) and the bundle commitment
    # BEFORE the handshake - fail-closed, no directory lookup (C1/C2/H3).
    if GG.pq_invites_enabled():
        resp.update(
            {
                "jti": info["jti"],
                "idm": info.get("idm"),
                "full_pubkey": info.get("operator_pubkey"),
                "ik_fp": info.get("ik_fp"),
                "bc": info.get("bc"),
                "mode": info.get("mode"),
                "operator_sig": info.get("operator_sig"),
                # Phase 2: the operator's current signed hybrid prekey, so the guest
                # can verify_commitment(full_pubkey, signed_prekey, bc) and encapsulate
                # its PQXDH to it. Read live (no JWT bloat). A prekey rotation since
                # mint makes bc mismatch, so the guest aborts (fail-closed, correct).
                "signed_prekey": _operator_signed_prekey(),
            }
        )
    return JSONResponse(resp)


# --------------------------------------------------------------------------- #
# Guest: join (create/lookup untrusted member → session token + call token)
# --------------------------------------------------------------------------- #
@router.post("/guest/join")
async def guest_join(request: Request):
    """Validate an invite, add the guest as an untrusted member, return tokens.

    Body: ``{invite_token, display_name, guest_pubkey}`` (Phase 1 additionally
    requires ``guest_sig`` binding the guest key to ``{jti, guest_pubkey, bc}``).
    Returns a guest session token scoped to ONLY the invite's group + a LiveKit
    guest call token + the group bootstrap (id/name + initial history). The
    invite's single-use claim is burned here.
    """
    _require_flag_guest()
    try:
        body = await request.json()
    except Exception:
        body = {}
    invite_token = (body.get("invite_token") or "").strip()
    display_name = (body.get("display_name") or "").strip()
    guest_pubkey = (body.get("guest_pubkey") or "").strip()
    guest_sig = (body.get("guest_sig") or "").strip()
    if not invite_token:
        raise HTTPException(400, "invite_token is required")
    if not display_name:
        raise HTTPException(400, "display_name is required")

    # Peek the operator claims BEFORE burning - needed for the Phase 1 guest-
    # binding check below AND for the S3 re-entry decision after the burn
    # attempt. ``check_used=False`` so a burned single-use jti still peeks
    # cleanly (only signature/tier/revocation/expiry/audience gate the peek).
    try:
        peek = GG.verify_group_invite(invite_token, burn_single_use=False, check_used=False)
    except GG.InviteInvalid as exc:
        logger.info("guest-group join rejected (peek): %s", exc)
        raise HTTPException(401, "invalid or expired invite") from exc

    # Phase 1: the guest-binding check runs regardless of the invite's burned
    # state, so it also gates the S3 re-entry path - a stolen/replayed link
    # presented by a third party who lacks the guest key is rejected the same
    # as a first-time join.
    if GG.pq_invites_enabled():
        from skchat import pq_invites as PQI

        bc = peek.get("bc")
        # Bind the guest browser key to THIS invite; a stolen link replayed by a
        # third party who lacks the guest key → 401 (generic, no oracle).
        if not (
            guest_pubkey
            and guest_sig
            and bc
            and PQI.verify_guest_binding(guest_sig, guest_pubkey, peek["jti"], bc)
        ):
            logger.info("guest-group join rejected: guest key binding failed")
            raise HTTPException(401, "invalid or expired invite")

    try:
        info = GG.verify_group_invite(invite_token, burn_single_use=True)
    except GG.InviteInvalid as exc:
        # S3 re-entry: a single-use DM invite whose jti is already burned may
        # still admit back the SAME previously-admitted guest - their session
        # JWT outlived the invite's own (already-consumed) link. Any other
        # presenter falls through to the same generic 401 below (no oracle).
        reentry = _dm_reentry(peek, guest_pubkey, request)
        if reentry is not None:
            return reentry
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

    is_dm = group.metadata.get("mode") == "dm"
    if is_dm and info.get("dm_reuse"):
        # Reusable my-DM-link (S1 marker): fan out per distinct guest key - a
        # returning fp resolves to its own existing DM (registry lookup), a
        # brand-new fp lands in the anchor (first-ever arrival) or a freshly
        # minted sibling DM (each later arrival), rate-capped per invite jti.
        try:
            group = GG.resolve_dm_admission_group(info, fp)
        except GG.ContactRateLimited:
            logger.info("dm join rejected: contact rate limit for jti=%s", info["jti"])
            raise HTTPException(401, "invalid or expired invite")
        group_id = group.id
    elif (
        is_dm
        and group.get_member(guest_id) is None
        and group.member_count >= GG.DM_SEAT_CAP
    ):
        # Mode-A DM (single-use / non-reusable): a NEW guest that would take a
        # third seat is refused (the DM is full). A returning guest (same
        # identity) is idempotent and always allowed.
        logger.info("dm join rejected: %s full (%d seats)", group_id, group.member_count)
        raise HTTPException(403, "direct message is full")

    GG.add_untrusted_guest_member(group, guest_id, display)
    G.save_group(group)

    if is_dm:
        meta = GG.get_dm_invite_meta(info["jti"]) or {}
        GG.upsert_dm_contact(
            fp,
            guest_id=guest_id,
            group_id=group.id,
            invite_jti=info["jti"],
            alias=meta.get("alias"),
            contact_ttl=meta.get("contact_ttl"),
        )

    session = GG.mint_guest_session(group_id=group_id, guest_id=guest_id, name=display, fp=fp)

    # LiveKit guest call token (publish A/V + screen + subscribe, never admin) -
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


def _dm_reentry(peek: dict, guest_pubkey: str, request: Request):
    """S3: re-admit a returning guest whose single-use DM invite is burned.

    A single-use dm invite's session JWT can outlive the invite's own link (max
    7d each, minted independently), locking the guest out on a return visit
    even though nothing about the relationship changed. This mints a FRESH
    session for the SAME previously-admitted guest - no new seat, no group
    mutation - when ALL of the following hold against the (never-burning) peek
    of the presented token:

      * the invite is single-use and its jti is in fact already burned (the
        only reason ``verify_group_invite(burn_single_use=True)`` just failed);
      * it names a ``mode="dm"`` group that still exists;
      * the presenting ``guest_pubkey``'s fingerprint has an ACTIVE (not
        revoked/expired) ``dm_contacts`` row bound to that exact jti + group;
      * that contact's guest_id is already a member of the group.

    Returns ``None`` on any mismatch (wrong key, stranger, revoked/expired
    contact, non-dm group, vanished group/membership) so the caller falls
    through to the same generic 401 every other invalid presenter gets - no
    oracle distinguishing "almost, but not quite" from "never valid".
    """
    if not peek.get("single_use"):
        return None
    from skchat.guest import _is_used

    jti = peek["jti"]
    if not _is_used(jti):
        return None  # burn failed for some OTHER reason - not a re-entry case

    from skchat import daemon_proxy_groups as G

    group = G.load_group(peek["group_id"])
    if group is None or group.metadata.get("mode") != "dm":
        return None

    fp = GG.pubkey_fingerprint(guest_pubkey)
    contact = GG.get_dm_contact(fp)
    if (
        contact is None
        or contact.get("invite_jti") != jti
        or contact.get("group_id") != group.id
        or contact.get("status") != "active"
    ):
        return None
    expires_at = contact.get("contact_expires_at")
    if expires_at is not None and float(expires_at) <= time.time():
        return None

    guest_id = contact["guest_id"]
    member = group.get_member(guest_id)
    if member is None:
        return None

    session = GG.mint_guest_session(
        group_id=group.id, guest_id=guest_id, name=member.display_name, fp=fp
    )
    # Bump last_seen_at only - no group/member mutation, alias/expiry untouched.
    GG.upsert_dm_contact(fp, guest_id=guest_id, group_id=group.id, invite_jti=jti)
    call = _mint_guest_call_token(group.id, guest_id, member.display_name, request)
    bootstrap = _guest_messages(group.id, limit=200, guest_id=guest_id)
    logger.info("dm re-entry: guest %s resumed via burned jti=%s", guest_id, jti)
    return JSONResponse(
        {
            "ok": True,
            "session_token": session,
            "guest_id": guest_id,
            "display_name": member.display_name,
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
    except Exception as exc:  # noqa: BLE001 - degrade, never 500 the join
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
    "no pre-epoch history"). Non-dm groups are NOT fenced - existing group-invite
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
    """Return the bound group's thread (token-scoped). No group id is accepted -
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
    recorded as advisory metadata - it proves same-browser continuity, not
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
    # Authoritative log: the ONE canonical group event, not the member copies.
    hist.record_event(group_msg)
    # Per-member history copies (legacy 1:1-style inbox). Redundant once the
    # authoritative log is on (record_event above logs the canonical event once);
    # skip them then to stop the 1->N write amplification. Flag off => legacy.
    _log_on = os.getenv("SKCHAT_MESSAGE_LOG", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )
    for member in group.members:
        if member.identity_uri == session.guest_id or _log_on:
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
    # it - a guest must not be able to react to a message in another room even
    # with a guessed id.
    from skchat import daemon_proxy_groups as G

    thread_ids = {
        getattr(m, "id", None) for m in G.group_thread_messages(hist, session.group_id, limit=2000)
    }
    if message_id not in thread_ids:
        raise HTTPException(403, "message is not in your room")

    msg = (
        hist.set_reaction(message_id, emoji, session.guest_id)
        if op == "add"
        else hist.clear_reaction(message_id, emoji, session.guest_id)
    )
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
    """Download a file - ONLY if it belongs to the guest's bound group.

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


# --------------------------------------------------------------------------- #
# Guest: rename self (change display name in the bound group)
# --------------------------------------------------------------------------- #
@router.post("/guest/name")
async def guest_rename(request: Request):
    """Change the caller's own display name in the bound group.

    Body: ``{name}``. Reserved names are suffixed (``enforce_display_name``,
    same anti-impersonation as join). The member row is refreshed in place via
    the idempotent ``add_untrusted_guest_member`` - ``guest_id`` (and so
    history attribution, the epoch fence, and the ``dm_contacts`` fp mapping)
    stays exactly as-is; only the display changes. The guest session is then
    REMINTED, since the display name is baked into the session JWT claims
    (``GuestSession.name`` - see ``mint_guest_session``): a rename without a
    remint would silently revert on the guest's very next request. The OLD
    token keeps working until it naturally expires, but still carries the old
    name.
    """
    _require_flag_guest()
    session = _guest_session(request)
    group = _bound_group(session)

    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_name = (body.get("name") or "").strip()
    if not raw_name:
        raise HTTPException(400, "name is required")

    display = GG.enforce_display_name(raw_name)
    GG.add_untrusted_guest_member(group, session.guest_id, display)

    from skchat import daemon_proxy_groups as G

    G.save_group(group)

    new_session = GG.mint_guest_session(
        group_id=session.group_id, guest_id=session.guest_id, name=display, fp=session.fp
    )
    logger.info("guest rename: guest=%s group=%s", session.guest_id, session.group_id)
    return JSONResponse({"ok": True, "display_name": display, "session_token": new_session})


# --------------------------------------------------------------------------- #
# Mode C: non-federated accept/sign (a peer that already has an identity)      #
# --------------------------------------------------------------------------- #
# A peer WITH an identity accepts an invite by signing an accept assertion; the
# operator reviews it (SAS) and counter-signs a mutual join record. The crypto
# lives in guest_accept.py; these routes carry it over the Funnel (the gift-wrap
# rendezvous is an alternative transport). Pending assertions are held in memory
# keyed by jti between accept and counter-sign.
_mode_c_pending: dict = {}
_mode_c_lock = threading.Lock()


def _mode_c_sas(bc: str, operator_fp: str, peer_fp: str) -> str:
    """6-digit Short Authentication String over bc + both bundle fingerprints.

    Both sides compute it and compare out-of-band; a MITM key swap changes a
    fingerprint so the SAS mismatches.
    """
    h = hashlib.sha256(f"{bc}|{operator_fp}|{peer_fp}".encode()).digest()
    return f"{int.from_bytes(h[:4], 'big') % 1_000_000:06d}"


def _mode_c_admit(pend: dict, operator_id: str = ""):
    """Build + operator-sign the mutual join_record, burn the invite nonce (H5),
    persist the admission (with the peer's operator_id for Mode B), and admit the
    peer to the group. Shared by manual counter-sign and trust-inherited auto-
    admit. Returns ``(join_record, sig_operator)``.
    """
    import json as _json

    from skchat import crypto as _crypto
    from skchat import daemon_proxy_groups as G
    from skchat import guest_accept as A

    chat_crypto = _crypto.load_agent_crypto()
    if chat_crypto is None or not getattr(chat_crypto, "can_sign", False):
        raise HTTPException(500, "operator signing key unavailable")
    op_fp = A.pubkey_fingerprint(str(chat_crypto._private_key.pubkey))
    ts = int(time.time())
    record = A.build_join_record(
        pend["jti"],
        op_fp,
        pend["peer_fp"],
        op_fp,
        pend["peer_fp"],
        pend["assertion"],
        pend["sig_peer"],
        ts,
    )
    sig_operator = A.sign_join_record(chat_crypto, record)

    nonces = A.ConsumedNonces()
    try:
        nonces.mark_consumed(pend["jti"])
        nonces.record_admission(
            pend["peer_fp"], operator_id, _json.dumps(record), sig_operator, pend["sig_peer"]
        )
    finally:
        nonces.close()

    try:
        group = G.load_group(pend["group_id"])
        if group is not None:
            GG.add_untrusted_guest_member(group, f"peer:{pend['peer_fp'][:16]}", "Peer")
            G.save_group(group)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mode-c admit failed: %s", exc)
    return record, sig_operator


def _process_mode_c_accept(body: dict):
    """Core Mode C accept processing, shared by the direct route and the
    gift-wrapped route: verify the invite + the peer's assertion, inherit trust
    if the peer proves membership under an opt-in-trusted operator (Mode B), else
    hold pending for manual review. Returns a JSONResponse; raises HTTPException
    (400/401) fail-closed."""
    from skchat import guest_accept as A

    invite_token = (body.get("invite_token") or "").strip()
    assertion = body.get("accept_assertion") or {}
    sig_peer = (body.get("sig_peer") or "").strip()
    accepter_pubkey = (body.get("accepter_pubkey") or "").strip()
    if not (invite_token and assertion and sig_peer and accepter_pubkey):
        raise HTTPException(400, "accept_assertion, sig_peer, accepter_pubkey required")

    try:
        info = GG.verify_group_invite(invite_token, burn_single_use=False)
    except GG.InviteInvalid as exc:
        logger.info("mode-c accept rejected (invite): %s", exc)
        raise HTTPException(401, "invalid or expired invite") from exc

    bc = info.get("bc")
    if not bc or not A.verify_accept_assertion(
        assertion, sig_peer, accepter_pubkey, expected_bc=bc
    ):
        logger.info("mode-c accept rejected: assertion verify failed")
        raise HTTPException(401, "accept assertion verification failed")

    jti = info["jti"]
    peer_fp = A.pubkey_fingerprint(accepter_pubkey)
    pend = {
        "jti": jti,
        "group_id": info["group_id"],
        "assertion": assertion,
        "sig_peer": sig_peer,
        "accepter_pubkey": accepter_pubkey,
        "peer_fp": peer_fp,
        "bc": bc,
        "sas": _mode_c_sas(bc, info.get("ik_fp") or "", peer_fp),
        "ts": int(time.time()),
    }
    # Mode B: if the peer PROVES (an operator-signed attestation over its own key)
    # that it belongs to an OPT-IN-trusted peer-operator, inherit trust and
    # auto-admit (skip the SAS). Fail-closed: a self-declared operator_id with no
    # valid attestation, or an untrusted/revoked operator, falls through to manual
    # review, so a spoofed claim can NEVER inherit trust.
    operator_id = (body.get("operator_id") or "").strip()
    operator_attestation = (body.get("operator_attestation") or "").strip()
    if operator_id and operator_attestation:
        nonces = A.ConsumedNonces()
        try:
            op_pub = nonces.operator_pubkey(operator_id)  # trusted AND not revoked
        finally:
            nonces.close()
        if op_pub and A.verify_operator_attestation(op_pub, accepter_pubkey, operator_attestation):
            pend["operator_id"] = operator_id
            record, sig_operator = _mode_c_admit(pend, operator_id)
            return JSONResponse(
                {
                    "ok": True,
                    "jti": jti,
                    "inherited": True,
                    "join_record": record,
                    "sig_operator": sig_operator,
                }
            )

    with _mode_c_lock:
        _mode_c_pending[jti] = pend
    return JSONResponse({"ok": True, "jti": jti, "sas": pend["sas"], "peer_fp": peer_fp})


@router.post("/mode-c/accept-giftwrapped")
async def mode_c_accept_giftwrapped(request: Request):
    """A peer submits a Mode C accept sealed in a NIP-59 gift-wrap envelope, so a
    shared rendezvous relay sees only ciphertext + a throwaway key (H6 metadata
    privacy). Body: the gift-wrap ``envelope``. The operator opens it with its
    hybrid private key, then processes the inner accept exactly like the direct
    route. Fail-closed: an unopenable envelope is a generic 401."""
    _require_flag_guest()
    from skchat import guest_giftwrap as GW
    from skchat import pq_prekeys as PQ

    try:
        body = await request.json()
    except Exception:
        body = {}
    envelope = body.get("envelope") or body
    kp = PQ.ensure_agent_keypair()
    if not kp:
        raise HTTPException(500, "operator hybrid key unavailable")
    _, priv = kp
    try:
        inner = GW.open_giftwrap(envelope, priv.hex())
    except Exception as exc:  # noqa: BLE001 - any open failure is a generic reject
        logger.info("mode-c gift-wrapped accept rejected: %s", exc)
        raise HTTPException(401, "invalid gift-wrap envelope") from exc
    return _process_mode_c_accept(inner)


@router.post("/mode-c/accept")
async def mode_c_accept(request: Request):
    """A peer submits a signed accept assertion for an invite (Mode C, Phase 3).

    Body: ``{invite_token, accept_assertion, sig_peer, accepter_pubkey}``. Verifies
    the invite + the peer's assertion signature (bc anti-downgrade, aud==peer,
    scope), holds it pending for operator review, and returns the SAS. Fail-closed:
    a bad invite / assertion / bc is a generic 401 (no oracle).
    """
    _require_flag_guest()
    try:
        body = await request.json()
    except Exception:
        body = {}
    return _process_mode_c_accept(body)


@router.get("/mode-c/pending")
async def mode_c_pending(request: Request):
    """Operator: list Mode C accept assertions awaiting counter-sign."""
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)
    with _mode_c_lock:
        items = [
            {
                "jti": p["jti"],
                "group_id": p["group_id"],
                "peer_fp": p["peer_fp"],
                "sas": p["sas"],
                "ts": p["ts"],
            }
            for p in _mode_c_pending.values()
        ]
    return JSONResponse({"pending": items})


@router.post("/mode-c/counter-sign")
async def mode_c_counter_sign(request: Request):
    """Operator: counter-sign a pending accept assertion (Mode C, Phase 3).

    Body: ``{jti}``. Builds the mutually-signed join record, burns the invite
    nonce (H5), and admits the peer to the group. Operator-gated.
    """
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)

    try:
        body = await request.json()
    except Exception:
        body = {}
    jti = (body.get("jti") or "").strip()
    with _mode_c_lock:
        pend = _mode_c_pending.get(jti)
    if not pend:
        raise HTTPException(404, "no pending accept for that jti")

    record, sig_operator = _mode_c_admit(pend, pend.get("operator_id", ""))
    with _mode_c_lock:
        _mode_c_pending.pop(jti, None)
    return JSONResponse(
        {"ok": True, "jti": jti, "join_record": record, "sig_operator": sig_operator}
    )


@router.get("/mode-c/admitted")
async def mode_c_admitted(request: Request):
    """Operator: list durably-admitted peers (the TOFU pin store), newest first.

    Excludes any whose peer or operator pin has been revoked. Operator-gated.
    """
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)
    from skchat import guest_accept as A

    nonces = A.ConsumedNonces()
    try:
        items = [
            {
                "peer_fp": a["peer_fp"],
                "operator_id": a["operator_id"],
                "admitted_at": a["admitted_at"],
            }
            for a in nonces.list_admissions()
        ]
    finally:
        nonces.close()
    return JSONResponse({"admitted": items})


@router.post("/mode-c/revoke")
async def mode_c_revoke(request: Request):
    """Operator: revoke an admitted peer's trust pin (H5). Body: ``{peer_fp}``.

    Revokes the identity pin, so its join record no longer counts and the peer
    drops out of the admitted list. Operator-gated.
    """
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)
    from skchat import guest_accept as A

    try:
        body = await request.json()
    except Exception:
        body = {}
    peer_fp = (body.get("peer_fp") or "").strip()
    if not peer_fp:
        raise HTTPException(400, "peer_fp is required")
    nonces = A.ConsumedNonces()
    try:
        nonces.revoke_pin(peer_fp)
    finally:
        nonces.close()
    return JSONResponse({"ok": True, "revoked": peer_fp})


@router.post("/mode-c/trust-operator")
async def mode_c_trust_operator(request: Request):
    """Operator: EXPLICITLY opt-in to trust a peer-operator (Mode B trust
    inheritance). Body: ``{operator_id, operator_pubkey}``. An agent that later
    presents an attestation signed by this operator over its own key is admitted
    without a fresh SAS. Never implicit (H4). Operator-gated."""
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)
    from skchat import guest_accept as A

    try:
        body = await request.json()
    except Exception:
        body = {}
    operator_id = (body.get("operator_id") or "").strip()
    operator_pubkey = (body.get("operator_pubkey") or "").strip()
    if not (operator_id and operator_pubkey):
        raise HTTPException(400, "operator_id and operator_pubkey are required")
    nonces = A.ConsumedNonces()
    try:
        nonces.trust_operator(operator_id, operator_pubkey)
    finally:
        nonces.close()
    return JSONResponse({"ok": True, "trusted": operator_id})


@router.get("/mode-c/trusted-operators")
async def mode_c_trusted_operators(request: Request):
    """Operator: list opt-in-trusted peer-operators (non-revoked)."""
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)
    from skchat import guest_accept as A

    nonces = A.ConsumedNonces()
    try:
        items = nonces.list_trusted_operators()
    finally:
        nonces.close()
    return JSONResponse({"trusted_operators": items})


@router.post("/mode-c/untrust-operator")
async def mode_c_untrust_operator(request: Request):
    """Operator: revoke trust in a peer-operator (H5). Body: ``{operator_id}``.
    Its agents stop inheriting and it drops from the trusted list."""
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)
    from skchat import guest_accept as A

    try:
        body = await request.json()
    except Exception:
        body = {}
    operator_id = (body.get("operator_id") or "").strip()
    if not operator_id:
        raise HTTPException(400, "operator_id is required")
    nonces = A.ConsumedNonces()
    try:
        nonces.revoke_pin(operator_id)
    finally:
        nonces.close()
    return JSONResponse({"ok": True, "untrusted": operator_id})


# --------------------------------------------------------------------------- #
# Operator: dm_contacts management surface (S4) - list / alias-mute-expiry PATCH
# / revoke. Mounted under /api/v1/guest-dm, which is NOT in
# dataplane_paths._EXEMPT_PREFIX (segment-boundary anchoring means
# "/api/v1/guest-dm" does not match the "/api/v1/guest" exemption), so these
# routes are gated by BOTH the in-route _require_operator check below and the
# dataplane middleware - see test_gate_middleware / test_guest_dm_contact_routes.
# --------------------------------------------------------------------------- #
def _dm_contact_guest_name(group_id: str, guest_id: str) -> str:
    """Best-effort display name for a dm_contacts row's guest_id."""
    from skchat import daemon_proxy_groups as G

    group = G.load_group(group_id) if group_id else None
    member = group.get_member(guest_id) if group is not None and guest_id else None
    return member.display_name if member is not None else ""


@router.get("/guest-dm/contacts")
async def guest_dm_contacts_list(request: Request):
    """Operator-only: list every ``dm_contacts`` row.

    Each entry: ``fp``, ``guest_name`` (resolved from the bound group's
    roster), ``alias``, ``group_id``, ``status``, ``muted``,
    ``contact_expires_at``, ``created_at``, ``last_seen_at``. The alias is
    operator-only metadata - never returned by any ``/guest/*`` route.
    """
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)

    contacts = [
        {
            "fp": c["fp"],
            "guest_name": _dm_contact_guest_name(c.get("group_id", ""), c.get("guest_id", "")),
            "alias": c.get("alias"),
            "group_id": c.get("group_id"),
            "status": c.get("status"),
            "muted": bool(c.get("muted")),
            "contact_expires_at": c.get("contact_expires_at"),
            "created_at": c.get("created_at"),
            "last_seen_at": c.get("last_seen_at"),
        }
        for c in GG.list_dm_contacts()
    ]
    return JSONResponse({"contacts": contacts})


@router.patch("/guest-dm/contacts/{fp}")
async def guest_dm_contact_patch(fp: str, request: Request):
    """Operator-only: partial-update a ``dm_contacts`` row.

    Body (all optional, partial update - omitted fields are left untouched):
    ``alias``, ``contact_ttl`` (seconds from now) or ``contact_expires_at``
    (absolute epoch seconds; ``contact_ttl`` wins if both are given), ``muted``.
    404 if no contact row exists for ``fp``.
    """
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)

    try:
        body = await request.json()
    except Exception:
        body = {}

    kwargs: dict = {}
    if "alias" in body:
        alias_raw = body.get("alias")
        kwargs["alias"] = (
            (str(alias_raw).strip() or None) if alias_raw not in (None, "") else None
        )
    if body.get("contact_ttl") is not None:
        try:
            ttl = int(body["contact_ttl"])
        except (TypeError, ValueError):
            raise HTTPException(400, "contact_ttl must be an integer")
        kwargs["contact_expires_at"] = time.time() + ttl
    elif "contact_expires_at" in body:
        expires_raw = body.get("contact_expires_at")
        kwargs["contact_expires_at"] = float(expires_raw) if expires_raw is not None else None
    if "muted" in body:
        kwargs["muted"] = bool(body.get("muted"))

    if not GG.update_dm_contact(fp, **kwargs):
        raise HTTPException(404, "contact not found")
    return JSONResponse({"ok": True, "contact": GG.get_dm_contact(fp)})


@router.post("/guest-dm/contacts/{fp}/revoke")
async def guest_dm_contact_revoke(fp: str, request: Request):
    """Operator-only: revoke a dm contact.

    Sets ``status='revoked'`` and revokes the invite ``jti`` that admitted it
    (``guest.revoke_invite``) - the S3 chokepoint
    (``_enforce_dm_contact_status``) then 403s the guest on every subsequent
    route. 404 if no contact row exists for ``fp``.
    """
    _require_flag_operator()
    from skchat.guest import _require_operator

    _require_operator(request)

    if not GG.revoke_dm_contact(fp):
        raise HTTPException(404, "contact not found")
    logger.info("dm contact revoked: fp=%s", fp)
    return JSONResponse({"ok": True, "revoked_fp": fp})


def register_guest_group_routes(app) -> None:
    """Register the guest-group router on the FastAPI app (called from webui.py)."""
    app.include_router(router)
