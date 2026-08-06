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


def pq_invites_enabled() -> bool:
    """True iff the Phase-1 signed-PQ-invite layer is on (thin re-export).

    Delegates to :func:`skchat.pq_invites.pq_invites_enabled` so route/handler
    code can gate on ``GG.pq_invites_enabled()`` alongside ``guest_links_enabled()``.
    """
    from skchat.pq_invites import pq_invites_enabled as _enabled

    return _enabled()


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
        "chef",
        "lumina",
        "opus",
        "jarvis",
        "ava",
        "sovereign",
        "admin",
        "host",
        "artisan",
        "herald",
        "sentinel",
        "architect",
        "scholar",
        "steward",
        "coder",
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
    mode: str = "group",
    aud: Optional[str] = None,
    scope: Optional[str] = None,
    dm_reuse: bool = False,
    now_fn=None,
) -> dict:
    """Mint a signed, room-scoped invite token for ``group_id``.

    Returns ``{token, join_url, jti, group_id, expires_at, ttl, single_use}``.
    ``join_url`` is **relative** (``/app/#/g/<token>``, the Flutter guest
    route) so it works behind any origin (tailnet/funnel) the operator
    shares from.

    When ``SKCHAT_PQ_INVITES_ENABLED`` is on (Phase 1), the token additionally
    carries the operator-signed identity claims (``idm``, ``bc``, ``mode`` + the
    operator's full inline pubkey and detached signature) and the ``join_url``
    gains the fragment-only 32-byte link secret ``&k=`` — see
    :mod:`skchat.pq_invites`. Assembly is fail-closed: if the operator identity /
    signing key / signed prekey cannot be resolved it raises (never emits an
    unsigned or classical-only invite). When the flag is off this function is
    byte-for-byte unchanged.
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
    # Macaroon-style caveats (Phase 3): narrow the invite to an audience (peer
    # FQID / fingerprint) and a permission scope. verify_group_invite enforces
    # them against a caller-supplied context, fail-closed (wrong aud -> 401).
    if aud:
        payload["aud"] = aud
    if scope:
        payload["scope"] = scope
    # A reusable my-DM-link (S1): the invite is never single-use and carries a
    # marker so a fresh arrival can be told "this is the standing link" (S2 does
    # the per-arrival fanout; this just plumbs the claim through mint + verify).
    if dm_reuse:
        payload["dm_reuse"] = True

    # Phase-1 PQ additions (flag-gated, fail-closed) — operator-signed identity
    # claims in the token + fragment-only link secret in the URL.
    fragment_secret = None
    pq_material = None
    from skchat import pq_invites as _pqi

    if _pqi.pq_invites_enabled():
        pq_material = _pqi.resolve_operator_material(mode)  # raises → fail-closed
        payload["idm"] = pq_material["idm"]
        payload["bc"] = pq_material["bc"]
        payload["mode"] = pq_material["mode"]
        payload["ik_fp"] = pq_material["ik_fp"]
        payload["op_sig"] = pq_material["operator_sig"]
        payload["op_pub"] = pq_material["operator_pubkey"]
        fragment_secret = _pqi.new_fragment_secret()

    token = _jwt.encode(payload, _secret(), algorithm="HS256")
    result = {
        "token": token,
        # Point at the Flutter app's guest route (hash-routed under /app/), NOT
        # /join/<token> — that collided with the old conf `/join/<room>?invite=`
        # page ("invite parameter is missing"). fullLink() prefixes the origin.
        # Every secret (token + k) stays after '#' (H7).
        "join_url": _pqi.build_join_url(token, fragment_secret),
        "jti": jti,
        "group_id": gid,
        "expires_at": exp,
        "ttl": eff_ttl,
        "single_use": single_use,
    }
    if pq_material is not None:
        result["mode"] = pq_material["mode"]
        result["bc"] = pq_material["bc"]
        result["idm"] = pq_material["idm"]
        result["fragment_secret"] = fragment_secret
    return result


# ── 1:1 DM invites (degenerate 2-seat guest group) ──────────────────────────
#: A ``mode="dm"`` guest group is a 1:1: it may ever hold at most two seats
#: (seat 1 = operator, seat 2 = the single peer guest). Enforced in ``guest_join``.
DM_SEAT_CAP = 2


def create_dm_invite(
    *,
    operator_uri: Optional[str] = None,
    ttl: Optional[int] = None,
    single_use: bool = True,
    reusable: bool = False,
    now_fn=None,
) -> dict:
    """Mint a 1:1 DM invite as a degenerate 2-seat guest group (Mode A DM).

    Phase 0 of the sovereign invite/join architecture: a 1:1 is modelled as a
    guest group with exactly two seats and ``metadata.mode="dm"``, so the whole
    existing guest-group machinery (invite/join/scoping/isolation) is reused
    unchanged. This mints a fresh DM group with the operator in seat 1, tags it
    ``mode="dm"``, then issues a (single-use by default) invite for it.

    ``reusable=True`` (S1) mints the operator's standing "my-DM-link": it is
    NEVER single-use (overrides ``single_use``) and carries a ``dm_reuse=true``
    claim (surfaced by :func:`verify_group_invite`) alongside the anchor
    ``group_id`` of the 2-seat group minted for the first arrival — per-arrival
    fanout beyond that first seat lands in S2.

    ``operator_uri`` defaults to the running agent's sovereign identity. Returns
    the :func:`create_group_invite` dict augmented with ``mode="dm"`` (its
    ``group_id`` is the freshly-minted DM group's id).
    """
    from skchat import daemon_proxy_groups as G

    op = (operator_uri or "").strip()
    if not op:
        from skchat.identity_bridge import get_sovereign_identity

        op = get_sovereign_identity()

    grp = G.create_group(name="Direct message", creator_uri=op, members=[])
    grp.metadata["mode"] = "dm"
    G.save_group(grp)

    eff_single_use = False if reusable else single_use
    invite = create_group_invite(
        grp.id,
        ttl=ttl,
        single_use=eff_single_use,
        mode="dm",
        dm_reuse=reusable,
        now_fn=now_fn,
    )
    invite["mode"] = "dm"
    return invite


class InviteInvalid(Exception):
    """Raised when an invite token is invalid/expired/revoked/used/wrong-tier.

    Callers MUST map this to a generic 401/403 without leaking the detail (no
    oracle distinguishing expiry vs bad signature vs revoked).
    """


def verify_group_invite(
    token: str,
    *,
    burn_single_use: bool = True,
    expected_aud: Optional[str] = None,
    check_used: bool = True,
) -> dict:
    """Verify an invite token → ``{jti, group_id, exp, single_use}``.

    Raises :class:`InviteInvalid` for any bad/expired/revoked/used token. When
    ``burn_single_use`` is True a ``once`` invite is atomically burned here (so a
    second join loses the race) — pass False to peek without consuming (preview).

    ``check_used=False`` additionally skips the "already used" rejection for a
    single-use invite (signature/tier/revocation/expiry/audience are still fully
    enforced) — used by the S3 DM re-entry peek, which needs the invite's
    ``group_id``/``jti`` even though its single-use jti is already burned.
    ``burn_single_use`` still atomically fails on an already-used jti regardless
    of this flag (the primary-key insert is the real guard), so this can never
    be used to sneak a second burn through.
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
            options={
                "require": ["jti", "exp", "iat", "group_id", "tier"],
                # We enforce the macaroon `aud` caveat manually below (PyJWT would
                # otherwise hard-fail any aud-bearing token when no audience kwarg
                # is passed, breaking peek/preview callers).
                "verify_aud": False,
            },
        )
    except PyJWTError as exc:
        raise InviteInvalid(f"invite decode failed: {exc}") from exc

    if payload.get("tier") != _INVITE_TIER:
        raise InviteInvalid("not a group-invite token")
    # Macaroon `aud` caveat (Phase 3): an audience-scoped invite is only valid for
    # the intended presenter. Fail-closed: a mismatch (or a missing context when
    # the invite demands one) is a generic InviteInvalid, no oracle.
    tok_aud = payload.get("aud")
    if tok_aud is not None and tok_aud != expected_aud:
        raise InviteInvalid("invite audience mismatch")
    gid = (payload.get("group_id") or "").strip()
    if not gid:
        raise InviteInvalid("invite missing group_id")
    jti = payload["jti"]
    if _is_revoked(jti):
        raise InviteInvalid(f"invite {jti!r} revoked")
    exp = float(payload["exp"])
    single_use = bool(payload.get("once"))
    if single_use:
        if check_used and _is_used(jti):
            raise InviteInvalid(f"single-use invite {jti!r} already used")
        if burn_single_use and not _mark_used(jti, expires_at=exp):
            raise InviteInvalid(f"single-use invite {jti!r} already used")
    result = {"jti": jti, "group_id": gid, "exp": exp, "single_use": single_use}
    # Surface the Phase-1 operator-signed claims when present (flag-gated mint).
    # Backward compatible: classic invites carry none of these keys.
    for src, dst in (
        ("idm", "idm"),
        ("bc", "bc"),
        ("mode", "mode"),
        ("ik_fp", "ik_fp"),
        ("op_sig", "operator_sig"),
        ("op_pub", "operator_pubkey"),
        ("aud", "aud"),
        ("scope", "scope"),
        ("dm_reuse", "dm_reuse"),
    ):
        val = payload.get(src)
        if val is not None:
            result[dst] = val
    return result


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
    *,
    group_id: str,
    guest_id: str,
    name: str,
    fp: str,
    ttl: Optional[int] = None,
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dm_invite_meta ("
        "  jti TEXT PRIMARY KEY,"
        "  alias TEXT,"
        "  contact_ttl INTEGER,"
        "  created_at REAL NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dm_contacts ("
        "  fp TEXT PRIMARY KEY,"
        "  guest_id TEXT,"
        "  group_id TEXT,"
        "  invite_jti TEXT,"
        "  alias TEXT,"
        "  contact_expires_at REAL,"
        "  status TEXT,"
        "  muted INTEGER,"
        "  created_at REAL NOT NULL,"
        "  last_seen_at REAL"
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


# ── DM invite sidecar (pre-set alias + contact-expiry TTL, jti-keyed) ───────
# SPEC CORRECTION: alias must NEVER go into the JWT payload (the HS256 payload
# is base64-decodable by the guest) and never into any /guest/* response — it
# is operator-only metadata, kept server-side in this sidecar table and read
# back only by operator-gated code paths.


def store_dm_invite_meta(
    jti: str, *, alias: Optional[str] = None, contact_ttl: Optional[int] = None
) -> None:
    """Persist an invite's pre-set alias / contact-expiry TTL, keyed by ``jti``."""
    j = (jti or "").strip()
    if not j:
        return
    with _store_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO dm_invite_meta (jti, alias, contact_ttl, created_at)"
                " VALUES (?, ?, ?, ?)",
                (j, alias, contact_ttl, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def get_dm_invite_meta(jti: str) -> Optional[dict]:
    """Return ``{alias, contact_ttl}`` for an invite's ``jti``, or None if unset."""
    j = (jti or "").strip()
    if not j:
        return None
    with _store_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT alias, contact_ttl FROM dm_invite_meta WHERE jti = ?", (j,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return {"alias": row[0], "contact_ttl": row[1]}


# ── Guest contact registry (S2: reusable-link per-guest DM fanout) ──────────
# ``dm_contacts`` is keyed by ``fp`` (the stable browser-key fingerprint, see
# ``pubkey_fingerprint``) — one row per distinct guest, regardless of which
# ``mode=dm`` invite it walked in through. This is what lets a returning guest
# on a reusable my-DM-link find its way back to its own 2-seat group + history,
# and what a fresh arrival's fanout gets recorded into.

_DM_CONTACT_RATE_ENV = "SKCHAT_DM_CONTACT_RATE_LIMIT"
_DEFAULT_DM_CONTACT_RATE = 20
_DM_CONTACT_RATE_WINDOW = 86400  # 24h


class ContactRateLimited(Exception):
    """Raised when minting another NEW ``dm_contacts`` row for an invite jti
    would exceed the per-invite rate cap. Callers MUST map this to a generic
    401 (no oracle distinguishing rate-limit from any other invite failure).
    """


def _dm_contact_rate_limit() -> int:
    try:
        v = int(os.getenv(_DM_CONTACT_RATE_ENV, str(_DEFAULT_DM_CONTACT_RATE)))
    except (TypeError, ValueError):
        v = _DEFAULT_DM_CONTACT_RATE
    return max(1, v)


def check_new_contact_allowed(jti: str, *, now_fn=None) -> bool:
    """True iff another NEW ``dm_contacts`` row may be created for ``jti``.

    Counts distinct contacts already created for this invite jti within the
    rolling 24h window (default cap 20, ``SKCHAT_DM_CONTACT_RATE_LIMIT``). A
    returning guest (an UPDATE, not an INSERT) never consumes this budget.
    """
    j = (jti or "").strip()
    if not j:
        return True
    now = float((now_fn or time.time)())
    cutoff = now - _DM_CONTACT_RATE_WINDOW
    with _store_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM dm_contacts WHERE invite_jti = ? AND created_at > ?",
                (j, cutoff),
            ).fetchone()
        finally:
            conn.close()
    count = row[0] if row else 0
    return count < _dm_contact_rate_limit()


def upsert_dm_contact(
    fp: str,
    *,
    guest_id: str,
    group_id: str,
    invite_jti: str,
    alias: Optional[str] = None,
    contact_ttl: Optional[int] = None,
    now_fn=None,
) -> None:
    """Insert or refresh a guest's ``dm_contacts`` row.

    A returning guest (same ``fp``) has ``last_seen_at`` bumped and its
    ``group_id``/``invite_jti`` refreshed; ``alias``/``contact_expires_at`` are
    only overwritten when THIS admission's sidecar actually carries them, so a
    plain rejoin (whose invite has no alias set) never clobbers a
    previously-recorded one.
    """
    f = (fp or "").strip()
    if not f:
        return
    now = float((now_fn or time.time)())
    new_expires = (now + contact_ttl) if contact_ttl is not None else None
    with _store_lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT alias, contact_expires_at FROM dm_contacts WHERE fp = ?", (f,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO dm_contacts (fp, guest_id, group_id, invite_jti, alias,"
                    " contact_expires_at, status, muted, created_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?)",
                    (f, guest_id, group_id, invite_jti, alias, new_expires, now, now),
                )
            else:
                final_alias = alias if alias is not None else existing[0]
                final_expires = new_expires if new_expires is not None else existing[1]
                conn.execute(
                    "UPDATE dm_contacts SET guest_id = ?, group_id = ?, invite_jti = ?,"
                    " alias = ?, contact_expires_at = ?, last_seen_at = ? WHERE fp = ?",
                    (guest_id, group_id, invite_jti, final_alias, final_expires, now, f),
                )
            conn.commit()
        finally:
            conn.close()


def get_dm_contact(fp: str) -> Optional[dict]:
    """Return the ``dm_contacts`` row for ``fp`` as a dict, or None."""
    f = (fp or "").strip()
    if not f:
        return None
    with _store_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT fp, guest_id, group_id, invite_jti, alias, contact_expires_at,"
                " status, muted, created_at, last_seen_at FROM dm_contacts WHERE fp = ?",
                (f,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    keys = (
        "fp",
        "guest_id",
        "group_id",
        "invite_jti",
        "alias",
        "contact_expires_at",
        "status",
        "muted",
        "created_at",
        "last_seen_at",
    )
    return dict(zip(keys, row))


def revoke_dm_contact(fp: str) -> bool:
    """Revoke a guest contact (S3 semantics; the operator-facing route is S4).

    Marks the ``dm_contacts`` row ``status='revoked'`` — the S3 enforcement
    chokepoint (``guest_group_routes._enforce_dm_contact_status``) then 403s
    every guest route for this fp on its next request, and re-entry via a
    burned single-use jti is blocked (the re-entry check requires an active
    contact). Also revokes the invite ``jti`` that admitted this contact
    (``guest.revoke_invite``) so the link itself dies, not just this session.

    Returns True iff a contact row existed for ``fp`` (False is a no-op).
    """
    from skchat.guest import revoke_invite

    f = (fp or "").strip()
    if not f:
        return False
    contact = get_dm_contact(f)
    if contact is None:
        return False
    with _store_lock:
        conn = _connect()
        try:
            conn.execute("UPDATE dm_contacts SET status = 'revoked' WHERE fp = ?", (f,))
            conn.commit()
        finally:
            conn.close()
    jti = contact.get("invite_jti")
    if jti:
        revoke_invite(jti)
    return True


def resolve_dm_admission_group(info: dict, fp: str, *, now_fn=None):
    """Resolve which 2-seat DM group a guest ``fp`` lands in on a REUSABLE
    dm invite (``info["dm_reuse"]``): its existing contact's group (a return
    visit), the anchor group (the invite's own ``group_id``, when a fresh
    arrival is the first ever), or a freshly minted sibling DM group (mirrors
    ``create_dm_invite`` internals) for each later distinct arrival.

    Raises :class:`ContactRateLimited` if a brand-new contact for this jti
    would exceed the per-invite creation rate cap.
    """
    from skchat import daemon_proxy_groups as G

    jti = (info.get("jti") or "").strip()
    contact = get_dm_contact(fp)
    if contact is not None:
        group = G.load_group(contact["group_id"])
        if group is not None:
            return group
        # The contact's group vanished — fall through and treat as fresh.

    if not check_new_contact_allowed(jti, now_fn=now_fn):
        raise ContactRateLimited(jti)

    anchor = G.load_group(info["group_id"])
    if anchor is not None and anchor.member_count < DM_SEAT_CAP:
        return anchor

    operator_uri = anchor.admin_uris[0] if anchor is not None and anchor.admin_uris else ""
    if not operator_uri:
        from skchat.identity_bridge import get_sovereign_identity

        operator_uri = get_sovereign_identity()

    group = G.create_group(name="Direct message", creator_uri=operator_uri, members=[])
    group.metadata["mode"] = "dm"
    G.save_group(group)
    return group


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
    # ``added_at`` is preserved across rejoins (a returning guest keeps its
    # original epoch-fence cutoff, else a rejoin would hide its own history —
    # see ``_dm_epoch_fence`` in guest_group_routes.py).
    guests = dict(group.metadata.get("guests") or {})
    prev_added_at = (guests.get(guest_id) or {}).get("added_at")
    guests[guest_id] = {
        "display": display,
        "trust": "untrusted",
        "guest": True,
        "added_at": prev_added_at if prev_added_at is not None else time.time(),
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
