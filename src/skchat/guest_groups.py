"""Guest GROUP access — one-link, group-scoped, full-in-room, UNTRUSTED guests.

This is the chat/file sibling of ``guest.py`` (which mints LiveKit-only invites
for conf *call* rooms). Here a single shareable link drops a recipient into ONE
specific group as an **untrusted guest** with full *in-room* functionality
(text, files, call, reactions) but **no admin/expansion powers**.

The whole surface is gated behind ``SKCHAT_GUEST_LINKS_ENABLED`` (default off →
the routes 404/403). No public ingress is wired — this is private-tailnet first.

Two JWTs (both HS256 over ``SKCHAT_GUEST_TOKEN_SECRET``, shared with guest.py):

* **invite token** — the link secret the operator sends out. Claims
  ``{jti, tier:"group-invite", group_id, iat, exp, once?}``. Room-scoped to the
  group; revocable (reuses ``guest.revoke_invite``); optional expiry/single-use.
* **guest session token** — minted on join, carried by the guest browser as a
  bearer token. Claims ``{jti, tier:"guest-session", group_id, guest_id, name,
  fp, iat, exp}``. **Scoped to exactly ONE group_id** — the request is pinned to
  this group server-side; any other group/conversation/file is 403.

Guest identity = ``guest:<slug>#<fp>`` where ``<fp>`` is the first 16 hex of
SHA-256 over the browser's exported SPKI public key (ECDSA P-256). The guest is
added to the group as an UNTRUSTED member (``metadata.guest=true``,
``trust="untrusted"``) so the roster + UI can badge them. Guests sign their
messages with the browser key; the signature is recorded as **advisory**
metadata (proves same-browser continuity, not capauth-verified identity).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skchat.guest_groups")

# ── Feature flag ────────────────────────────────────────────────────────────
_FLAG_ENV = "SKCHAT_GUEST_LINKS_ENABLED"


def guest_links_enabled() -> bool:
    """True iff the guest-group-link feature is enabled (default OFF).

    Accepts ``1/true/yes/on`` (case-insensitive). Everything guest-group is
    gated on this — when off the routes 404 (operator) / 403 (guest).
    """
    return os.getenv(_FLAG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# ── Token config (shared secret + TTLs) ─────────────────────────────────────
_GUEST_SECRET_ENV = "SKCHAT_GUEST_TOKEN_SECRET"
_INVITE_TTL_ENV = "SKCHAT_GROUP_INVITE_TTL"
_SESSION_TTL_ENV = "SKCHAT_GUEST_SESSION_TTL"

_DEFAULT_INVITE_TTL = 86400  # 24h
_MAX_INVITE_TTL = 7 * 86400  # 7 days hard cap
_DEFAULT_SESSION_TTL = 86400  # 24h guest session
_MAX_SESSION_TTL = 7 * 86400

_INVITE_TIER = "group-invite"
_SESSION_TIER = "guest-session"

# A guest LiveKit call token publishes A/V + screen + subscribe, never admin.
GUEST_CALL_TOKEN_TTL = 21600  # 6h

# Display names a guest may NOT claim verbatim (so they cannot impersonate an
# operator/agent in the roster). Reuses guest.py's set + the swarm agents.
_RESERVED_NAMES = frozenset(
    {
        "chef", "lumina", "opus", "jarvis", "ava", "sovereign", "admin", "host",
        "artisan", "herald", "sentinel", "architect", "scholar", "steward", "coder",
    }
)


def _secret() -> str:
    s = os.getenv(_GUEST_SECRET_ENV, "")
    if not s:
        raise RuntimeError(
            f"{_GUEST_SECRET_ENV} is not set. Generate one with: openssl rand -hex 32"
        )
    return s


def _invite_ttl() -> int:
    try:
        v = int(os.getenv(_INVITE_TTL_ENV, str(_DEFAULT_INVITE_TTL)))
    except (TypeError, ValueError):
        v = _DEFAULT_INVITE_TTL
    return min(max(60, v), _MAX_INVITE_TTL)


def _session_ttl() -> int:
    try:
        v = int(os.getenv(_SESSION_TTL_ENV, str(_DEFAULT_SESSION_TTL)))
    except (TypeError, ValueError):
        v = _DEFAULT_SESSION_TTL
    return min(max(60, v), _MAX_SESSION_TTL)


# ── Guest identity helpers ──────────────────────────────────────────────────
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    s = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return (s or "guest")[:32]


def pubkey_fingerprint(guest_pubkey: str) -> str:
    """Return a stable 16-hex fingerprint of the guest's exported public key.

    The browser exports its ECDSA P-256 public key as base64 SPKI; we hash the
    raw bytes (after stripping whitespace) so the SAME browser key always yields
    the SAME fingerprint → the SAME guest identity on a return visit.
    """
    raw = (guest_pubkey or "").strip()
    if not raw:
        raw = "anon"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def guest_identity(name: str, guest_pubkey: str) -> str:
    """Compose ``guest:<slug>#<fp>`` — the untrusted, self-asserted identity."""
    return f"guest:{_slug(name)}#{pubkey_fingerprint(guest_pubkey)}"


def enforce_display_name(name: str) -> str:
    """Suffix a reserved name so a guest cannot impersonate an operator/agent."""
    clean = (name or "").strip()[:40] or "Guest"
    if clean.lower() in _RESERVED_NAMES:
        return f"{clean} (guest)"[:40]
    return clean


# ── Invite tokens (operator → recipient link secret) ────────────────────────


def create_group_invite(
    group_id: str,
    *,
    ttl: Optional[int] = None,
    issuer: str = "operator",
    single_use: bool = False,
    now_fn=None,
) -> dict:
    """Mint a signed, room-scoped invite token for ``group_id``.

    Returns ``{token, join_url, jti, group_id, expires_at, ttl, single_use}``.
    ``join_url`` is **relative** (``/join/<token>``) so it works behind any
    origin (tailnet/funnel) the operator shares from.
    """
    import jwt as _jwt

    gid = (group_id or "").strip()
    if not gid:
        raise ValueError("group_id is required")
    eff_ttl = min(ttl or _invite_ttl(), _MAX_INVITE_TTL)
    now = float((now_fn or time.time)())
    exp = now + eff_ttl
    jti = secrets.token_hex(16)
    payload = {
        "jti": jti,
        "iss": issuer,
        "tier": _INVITE_TIER,
        "group_id": gid,
        "iat": int(now),
        "exp": int(exp),
    }
    if single_use:
        payload["once"] = True
    token = _jwt.encode(payload, _secret(), algorithm="HS256")
    return {
        "token": token,
        # Point at the Flutter app's guest route (hash-routed under /app/), NOT
        # /join/<token> — that collided with the old conf `/join/<room>?invite=`
        # page ("invite parameter is missing"). fullLink() prefixes the origin.
        "join_url": f"/app/#/g/{token}",
        "jti": jti,
        "group_id": gid,
        "expires_at": exp,
        "ttl": eff_ttl,
        "single_use": single_use,
    }


class InviteInvalid(Exception):
    """Raised when an invite token is invalid/expired/revoked/used/wrong-tier.

    Callers MUST map this to a generic 401/403 without leaking the detail (no
    oracle distinguishing expiry vs bad signature vs revoked).
    """


def verify_group_invite(token: str, *, burn_single_use: bool = True) -> dict:
    """Verify an invite token → ``{jti, group_id, exp, single_use}``.

    Raises :class:`InviteInvalid` for any bad/expired/revoked/used token. When
    ``burn_single_use`` is True a ``once`` invite is atomically burned here (so a
    second join loses the race) — pass False to peek without consuming (preview).
    """
    import jwt as _jwt
    from jwt.exceptions import PyJWTError

    # Revocation/used store is shared with guest.py (same JTI namespace).
    from skchat.guest import _is_revoked, _is_used, _mark_used

    try:
        payload = _jwt.decode(
            token,
            _secret(),
            algorithms=["HS256"],
            options={"require": ["jti", "exp", "iat", "group_id", "tier"]},
        )
    except PyJWTError as exc:
        raise InviteInvalid(f"invite decode failed: {exc}") from exc

    if payload.get("tier") != _INVITE_TIER:
        raise InviteInvalid("not a group-invite token")
    gid = (payload.get("group_id") or "").strip()
    if not gid:
        raise InviteInvalid("invite missing group_id")
    jti = payload["jti"]
    if _is_revoked(jti):
        raise InviteInvalid(f"invite {jti!r} revoked")
    exp = float(payload["exp"])
    single_use = bool(payload.get("once"))
    if single_use:
        if _is_used(jti):
            raise InviteInvalid(f"single-use invite {jti!r} already used")
        if burn_single_use and not _mark_used(jti, expires_at=exp):
            raise InviteInvalid(f"single-use invite {jti!r} already used")
    return {"jti": jti, "group_id": gid, "exp": exp, "single_use": single_use}


def jti_of(token: str) -> str:
    """Best-effort extract the ``jti`` of a token WITHOUT verifying signature.

    Used by the operator revoke route (``DELETE .../invite/{token}``) to find
    the JTI to revoke even from an expired token. Returns "" on any failure.
    """
    import jwt as _jwt

    try:
        payload = _jwt.decode(token, options={"verify_signature": False})
        return str(payload.get("jti") or "")
    except Exception:
        return ""


# ── Guest session tokens (server → guest browser bearer) ────────────────────


@dataclass
class GuestSession:
    """A validated guest session, pinned to exactly one group."""

    jti: str
    group_id: str
    guest_id: str
    name: str
    fp: str
    exp: float


def mint_guest_session(
    *, group_id: str, guest_id: str, name: str, fp: str, ttl: Optional[int] = None,
    now_fn=None,
) -> str:
    """Mint a guest session JWT scoped to exactly one ``group_id``."""
    import jwt as _jwt

    eff_ttl = min(ttl or _session_ttl(), _MAX_SESSION_TTL)
    now = float((now_fn or time.time)())
    payload = {
        "jti": secrets.token_hex(12),
        "tier": _SESSION_TIER,
        "group_id": group_id,
        "guest_id": guest_id,
        "name": name,
        "fp": fp,
        "iat": int(now),
        "exp": int(now + eff_ttl),
    }
    return _jwt.encode(payload, _secret(), algorithm="HS256")


class SessionInvalid(Exception):
    """Raised when a guest session token is invalid/expired/wrong-tier."""


def verify_guest_session(token: str) -> GuestSession:
    """Verify a guest session token → :class:`GuestSession` (or raise)."""
    import jwt as _jwt
    from jwt.exceptions import PyJWTError

    from skchat.guest import _is_revoked

    try:
        payload = _jwt.decode(
            token,
            _secret(),
            algorithms=["HS256"],
            options={"require": ["jti", "exp", "iat", "group_id", "guest_id", "tier"]},
        )
    except PyJWTError as exc:
        raise SessionInvalid(f"session decode failed: {exc}") from exc
    if payload.get("tier") != _SESSION_TIER:
        raise SessionInvalid("not a guest-session token")
    jti = payload["jti"]
    if _is_revoked(jti):
        raise SessionInvalid(f"session {jti!r} revoked")
    return GuestSession(
        jti=jti,
        group_id=(payload.get("group_id") or "").strip(),
        guest_id=(payload.get("guest_id") or "").strip(),
        name=(payload.get("name") or "Guest").strip(),
        fp=(payload.get("fp") or "").strip(),
        exp=float(payload["exp"]),
    )


# ── Per-group guest transfer allowlist (file download isolation) ────────────
# A guest may only download a transfer that was recorded as belonging to its
# bound group. Source of truth = a small SQLite table; survives restart.

_store_lock = threading.Lock()
_GUEST_DB_ENV = "SKCHAT_GUEST_GROUP_DB"
_DEFAULT_GUEST_DB = "~/.skchat/guest_groups.db"


def _db_path() -> Path:
    raw = os.getenv(_GUEST_DB_ENV, "").strip() or _DEFAULT_GUEST_DB
    return Path(raw).expanduser()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS group_transfers ("
        "  transfer_id TEXT PRIMARY KEY,"
        "  group_id TEXT NOT NULL,"
        "  created_at REAL NOT NULL"
        ")"
    )
    conn.commit()
    return conn


def record_group_transfer(transfer_id: str, group_id: str) -> None:
    """Record that ``transfer_id`` belongs to ``group_id`` (for guest download)."""
    tid = (transfer_id or "").strip()
    gid = (group_id or "").strip()
    if not tid or not gid:
        return
    with _store_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO group_transfers (transfer_id, group_id, created_at)"
                " VALUES (?, ?, ?)",
                (tid, gid, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def transfer_group(transfer_id: str) -> Optional[str]:
    """Return the group_id a transfer belongs to, or None if unknown."""
    tid = (transfer_id or "").strip()
    if not tid:
        return None
    with _store_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT group_id FROM group_transfers WHERE transfer_id = ?", (tid,)
            ).fetchone()
        finally:
            conn.close()
    return row[0] if row else None


# ── Untrusted-member roster integration ─────────────────────────────────────


def add_untrusted_guest_member(group, guest_id: str, display: str):
    """Add (or refresh) the guest as an UNTRUSTED member of ``group``.

    Idempotent: a returning guest (same identity) just refreshes their display
    name. The member is tagged ``metadata.guest=true`` / ``trust="untrusted"``
    via the group metadata sidecar (GroupMember has no free-form metadata, so we
    keep the guest registry in ``group.metadata['guests']``) and joins as an
    ordinary MEMBER so they can post in-room (full in-room functionality), never
    ADMIN.
    """
    from skchat.group import MemberRole, ParticipantType

    existing = group.get_member(guest_id)
    if existing is None:
        group.add_member(
            identity_uri=guest_id,
            role=MemberRole.MEMBER,
            participant_type=ParticipantType.HUMAN,
            display_name=display,
        )
    else:
        existing.display_name = display
        existing.role = MemberRole.MEMBER
    # Sidecar guest registry (untrusted markers the GroupMember model lacks).
    guests = dict(group.metadata.get("guests") or {})
    guests[guest_id] = {
        "display": display,
        "trust": "untrusted",
        "guest": True,
        "added_at": time.time(),
    }
    group.metadata["guests"] = guests
    return group


def is_guest_member(group, identity_uri: str) -> bool:
    """True if ``identity_uri`` is registered as an untrusted guest of ``group``."""
    if group is None:
        return False
    return identity_uri in (group.metadata.get("guests") or {})


# ── Advisory signature recording ────────────────────────────────────────────


def canonical_sign_payload(group_id: str, body: str, ts) -> str:
    """The exact string the guest browser signs (stable key order).

    ``ts`` may be a number or string; it is stringified verbatim so the bytes
    match what the browser produced (the server does not re-derive ts).
    """
    import json

    return json.dumps(
        {"body": body, "group_id": group_id, "ts": str(ts)},
        separators=(",", ":"),
        sort_keys=True,
    )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
