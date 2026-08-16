"""Fail-closed CapAuth gate for the chat data plane (P0.5 / SEAM 7).

The chat data-plane endpoints — ``POST /api/send``, ``POST /api/v1/prekey`` and
``GET /api/v1/inbox`` — historically shipped with **no** authentication: anyone
who can reach the port can send as the operator, publish a prekey, or read the
inbox. This module adds an **opt-in** CapAuth gate that mirrors the signature-
verification the call/signaling routes already do (a request must carry a valid
capauth credential or it is refused).

The gate is switched by the ``SKCHAT_DATAPLANE_AUTH`` env flag and defaults
**OFF** so the live app is not locked out before it starts signing its requests:

  * flag OFF (default) — endpoints behave exactly as before; the validator is
    never consulted and no credential is required.
  * flag ON — a missing **or** invalid capauth credential yields ``401``; a
    valid one passes through unchanged.

Validation is delegated to :class:`CapAuthValidator`, which is *injectable*
(``set_validator`` / ``get_validator``) so tests exercise the gate with capauth
fully mocked and production can wire the real verifier. The default validator
lazy-imports capauth (mirroring ``spaces/federation/assertion.py``) and **fails
closed** — any error resolving or running the verifier denies the request.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger("skchat.dataplane_auth")

ENV_FLAG = "SKCHAT_DATAPLANE_AUTH"
_TRUTHY = {"1", "true", "yes", "on"}

#: Opt-in flag for the third credential path: accepting a capauth audience-scoped
#: token minted for the skchat audience. Default OFF so the plane behaves exactly
#: as before (byte-identical) unless an operator explicitly enables it.
ACCEPT_AUDIENCE_ENV_FLAG = "SKCHAT_ACCEPT_AUDIENCE_TOKENS"

#: The capauth audience this dataplane accepts audience-scoped tokens for. A token
#: scoped to any other audience (or an unscoped legacy token) is never accepted
#: via the audience path.
SKCHAT_AUDIENCE = "skchat"

#: Opt-in flag for the backend audience-token MINT endpoint (POST
#: /api/v1/audience-token). Default OFF so the route is inert (404) and the app
#: is byte-identical to before this endpoint existed. When on, an AUTHENTICATED
#: caller can obtain a fresh audience-scoped token minted for THIS daemon's own
#: resolved identity (never a subject taken from request input).
AUDIENCE_MINT_ENV_FLAG = "SKCHAT_AUDIENCE_MINT"

#: Opt-in flag for the server-side issuer shadow (CR-3.4 P5 / Phase 1). Default
#: OFF, read at call time. When on, an HS256-operator-session-authenticated request
#: ALSO runs the audience path + PDP on a synthetic per-fingerprint twin and logs any
#: divergence (subject or decision). It NEVER changes the response, so the plane is
#: byte-identical whether it is on or off; it is pure observation.
ISSUER_SHADOW_ENV_FLAG = "SKCHAT_ISSUER_SHADOW"

#: The authz PDP staging flag (spec 3.5). off = authentication only, exactly as
#: today. shadow = also compute capauth.authz.decide(), log any divergence from
#: the legacy outcome, but RETURN THE LEGACY OUTCOME (no behavior change). enforce
#: = the PDP decision governs (authentication must still pass first). The flip to
#: enforce is Chef-gated on zero divergence over a 7-day window plus fixture replay.
AUTHZ_PDP_FLAG = "SKCHAT_AUTHZ_PDP"

# --------------------------------------------------------------------------- #
# Method-aware route -> capability map (SKWorld Authorization Model L2.3).
#
# Replaces the old suffix-only ``_CAP_BY_PATH`` (which mapped only 3 of 30+ gated
# routes: unmapped routes were invisible in shadow and 403 in enforce, the CR-3.3
# blocker / incident b). Every gated route now resolves to exactly one class:
#
#   * a capability   -> ``_ROUTE_CAPABILITY_RULES`` (below), OR
#   * self-auth      -> ``_SELF_AUTH_RULES`` (own verifier; the PDP is not consulted), OR
#   * public         -> ``PUBLIC_ROUTES`` (``dataplane_paths.is_gated`` returns False).
#
# The table is ``(method, path_pattern)`` keyed and shares matching semantics with
# ``dataplane_paths``: patterns are FULL-MATCH, ``{param}`` matches exactly one
# path segment, so the same rule matches both a concrete runtime path
# (``/file/abc123``) and the FastAPI ``path_format`` the coverage gate enumerates
# (``/file/{transfer_id}``). Method awareness is mandatory: ``GET /api/v1/inbox``
# is the operator reading their own inbox (``skchat.inbox``) while ``POST
# /api/v1/inbox`` is federation S2S delivery (self-auth, and left OUT of this
# table on purpose -- it is exempt in ``is_gated``).
#
# The completeness gate (``tests/test_dataplane_coverage.py``) enumerates the LIVE
# route table and fails if any gated route is neither capability-mapped nor
# self-auth, so a new gated route that skips classification breaks CI the same day.

#: Capability name constants (each equals a rule row in ``capauth.authz.DEFAULT_RULES``).
CAP_INBOX = "skchat.inbox"
CAP_STATUS = "skchat.status"
CAP_PREKEY = "skchat.prekey"
CAP_MEDIA_WRITE = "skchat.media.write"
CAP_VOICE = "skchat.voice"
CAP_SEND = "skchat.send"
CAP_GROUPS = "skchat.groups"
CAP_CALLS = "skchat.calls"

#: Ordered ``(method, path_pattern, capability)`` rows for every gated route that
#: maps to a capability. Order is not significant (patterns are full-match and a
#: concrete path routes to exactly one FastAPI route); grouped by capability for
#: review. The ``/members/self`` vs ``/members/{identity}`` pair both resolve to
#: ``skchat.groups``, so their (only) overlap is harmless.
_ROUTE_CAPABILITY_RULES: tuple[tuple[str, str, str], ...] = (
    # --- skchat.inbox (read own message content) ---------------------------- #
    ("GET", "/api/v1/inbox", CAP_INBOX),
    ("GET", "/api/v1/conversations", CAP_INBOX),
    ("GET", "/api/v1/conversations/{peer_id}", CAP_INBOX),  # incident (b) route
    ("GET", "/api/v1/thread/{thread_id}", CAP_INBOX),
    ("GET", "/api/v1/groups", CAP_INBOX),  # group READ (mutations -> skchat.groups)
    ("GET", "/api/v1/groups/{group_id}/members", CAP_INBOX),
    ("GET", "/file/{transfer_id}", CAP_INBOX),
    ("GET", "/file/{transfer_id}/thumb", CAP_INBOX),
    ("GET", "/media/file", CAP_INBOX),
    ("GET", "/api/v1/file_status", CAP_INBOX),  # transfer progress poll (read own)
    ("GET", "/inbox", CAP_INBOX),
    ("GET", "/messages", CAP_INBOX),
    ("GET", "/groups", CAP_INBOX),
    # --- skchat.status (read operational metadata) -------------------------- #
    ("GET", "/api/v1/status", CAP_STATUS),  # incident (b) route
    # Per-service backend health (card f2e6c451): leaks internal
    # hostnames/ports (STT/TTS/LLM/SFU targets), same bar as /api/v1/status.
    ("GET", "/api/v1/health", CAP_STATUS),
    ("GET", "/api/v1/peers", CAP_STATUS),
    ("GET", "/api/v1/household/agents", CAP_STATUS),
    ("GET", "/api/v1/geo/units", CAP_STATUS),
    ("GET", "/api/v1/webrtc/ice-config", CAP_STATUS),
    ("GET", "/api/v1/webrtc/peers", CAP_STATUS),
    ("GET", "/api/v1/groups/{group_id}/call/participants", CAP_STATUS),
    ("GET", "/agent/state", CAP_STATUS),
    ("GET", "/adapters", CAP_STATUS),
    ("GET", "/api/board", CAP_STATUS),  # interim; migrates to skboard.read (L1.8)
    ("GET", "/api/kanban", CAP_STATUS),  # interim; migrates to skboard.read (L1.8)
    ("GET", "/api/gtd", CAP_STATUS),  # GTD list view; interim, migrates to skboard.read
    ("GET", "/api/suggest/{surface}/{id}", CAP_STATUS),  # fleet suggest; interim
    ("POST", "/api/queue/{surface}/{id}", CAP_STATUS),  # fleet queue; interim
    ("GET", "/api/card/{card_id}", CAP_STATUS),  # interim; migrates to skboard.read
    ("GET", "/api/card/{card_id}/ai-suggestions", CAP_STATUS),  # AI next-step options
    # Kanban card mutation (move/assign/priority/label/note). A WRITE, so it must
    # be gated (an ungated route is public over the funnel). Interim CAP_STATUS
    # to avoid shipping an ungranted new cap that would lock the board off; must
    # migrate to skboard.write with the rest of the /api/board* family (L1.8).
    ("POST", "/api/card/{card_id}/{action}", CAP_STATUS),
    (
        "GET",
        "/api/v1/guest-dm/contacts",
        CAP_STATUS,
    ),  # operator-only: read the dm_contacts registry
    # --- skchat.prekey (publish/sign/delete own prekey bundles) ------------- #
    ("POST", "/api/v1/prekey", CAP_PREKEY),
    ("POST", "/api/v1/prekey/sign", CAP_PREKEY),
    ("DELETE", "/api/v1/prekey/{peer}/{key_id}", CAP_PREKEY),
    # Linked Devices: managing a device means managing its prekey slots, and an
    # enrolled device already holds skchat.prekey, so no new grant is needed.
    ("GET", "/api/v1/operator/devices", CAP_PREKEY),
    ("PATCH", "/api/v1/operator/devices/{device_fp}", CAP_PREKEY),
    ("DELETE", "/api/v1/operator/devices/{device_fp}", CAP_PREKEY),
    ("POST", "/api/v1/operator/devices/unlink-others", CAP_PREKEY),
    # Phase 3 (approval-to-link): approving/denying a device is the same
    # trust boundary as unlinking one, so it carries the same capability.
    ("GET", "/api/v1/operator/devices/pending", CAP_PREKEY),
    ("POST", "/api/v1/operator/devices/{device_fp}/approve", CAP_PREKEY),
    ("POST", "/api/v1/operator/devices/{device_fp}/deny", CAP_PREKEY),
    # --- skchat.media.write (upload attachment bytes) ----------------------- #
    ("POST", "/upload", CAP_MEDIA_WRITE),
    # --- skchat.voice (STT/TTS compute as the subject) ---------------------- #
    ("POST", "/api/v1/transcribe", CAP_VOICE),
    # --- skchat.send (act as the identity on the wire) ---------------------- #
    ("POST", "/api/send", CAP_SEND),
    ("POST", "/api/v1/send", CAP_SEND),
    ("POST", "/api/v1/react", CAP_SEND),
    ("POST", "/api/v1/edit", CAP_SEND),
    ("POST", "/api/v1/receipt", CAP_SEND),
    ("POST", "/api/v1/presence", CAP_SEND),
    ("POST", "/api/v1/dm/decrypt-failed", CAP_SEND),  # emits a signed re-pull request
    ("POST", "/send", CAP_SEND),
    # --- skchat.groups (mutate shared group state) -------------------------- #
    ("POST", "/api/v1/groups", CAP_GROUPS),
    ("PUT", "/api/v1/groups/{group_id}", CAP_GROUPS),
    ("DELETE", "/api/v1/groups/{group_id}", CAP_GROUPS),
    ("POST", "/api/v1/groups/{group_id}/members", CAP_GROUPS),
    ("DELETE", "/api/v1/groups/{group_id}/members/self", CAP_GROUPS),
    ("DELETE", "/api/v1/groups/{group_id}/members/{identity}", CAP_GROUPS),
    ("POST", "/api/v1/groups/{group_id}/invite", CAP_GROUPS),
    ("DELETE", "/api/v1/groups/{group_id}/invite/{token}", CAP_GROUPS),
    # operator-only dm_contacts registry mutations (partial-update, revoke invite jti)
    ("PATCH", "/api/v1/guest-dm/contacts/{fp}", CAP_GROUPS),
    ("POST", "/api/v1/guest-dm/contacts/{fp}/revoke", CAP_GROUPS),
    # per-group revoke variant (guest-dm G3): operator-only, same bar as above
    ("POST", "/api/v1/guest-dm/contacts/{fp}/groups/{group_id}/revoke", CAP_GROUPS),
    # whole-group expiry: locks every guest of the room out on a schedule, so
    # it carries the same bar as revoking them individually.
    ("PATCH", "/api/v1/guest-dm/groups/{group_id}", CAP_GROUPS),
    # --- skchat.calls (ring peers / join calls / mint LiveKit tokens) ------- #
    ("POST", "/api/v1/access/token", CAP_CALLS),  # mints a LiveKit token as the identity
    ("POST", "/api/v1/groups/{group_id}/call/start", CAP_CALLS),
    ("POST", "/api/v1/groups/{group_id}/call/join", CAP_CALLS),
)

#: Self-auth registry (L1.4): gated routes that authenticate on their OWN declared
#: terms and are never routed through the PDP. Each entry is
#: ``(method, path_pattern, verifier_name, rationale)``. The coverage gate accepts
#: a gated route via the capability table OR this registry -- never implicitly.
#: Both entries are token MINTS that call :func:`request_is_primary_authenticated`
#: unconditionally in-route (they must never mint for an anonymous caller, and CR-3.4
#: P3 requires a PRIMARY credential so an audience token can never mint another), so
#: the PDP has nothing to add.
_SELF_AUTH_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        "POST",
        "/api/v1/audience-token",
        "request_is_primary_authenticated",
        "Mints an audience-scoped token for THIS daemon's own resolved identity; "
        "in-route PRIMARY self-authentication, gated by SKCHAT_AUDIENCE_MINT.",
    ),
    (
        "POST",
        "/api/v1/embed-token",
        "request_is_primary_authenticated",
        "Mints the Grade-B embed pane token (rw scoped by SKCHAT_EMBED_RW_MODULES); "
        "in-route PRIMARY self-authentication.",
    ),
)

#: Explicit public allowlist (L1.3 / L1.4): routes deliberately served WITHOUT a
#: capability check, with a written rationale. These are the routes for which
#: ``dataplane_paths.is_gated`` returns False by design (health, static shell,
#: credential bootstrap, federation inbound, the public prekey directory, and the
#: guest/pair/livekit self-auth families). This structure documents the "why" and
#: lets the coverage gate assert nothing is BOTH public-listed and gated. It is a
#: curated mirror of ``dataplane_paths``' exempt tables, not an exhaustive
#: enumeration of every implicitly-public route (see L2.3 for the reclassification
#: follow-ups, e.g. /call/*, /spaces/*, /federation/status).
PUBLIC_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/health", "Liveness probe."),
    ("GET", "/api/health", "Liveness probe."),
    ("GET", "/favicon.ico", "Static shell asset."),
    ("GET", "/", "Redirect to /app."),
    ("GET", "/.well-known/skworld-module.json", "Module manifest served unauthenticated."),
    ("GET", "/api/v1/shell/modules", "Public multi-manifest discovery aggregate."),
    ("GET", "/api/v1/identity", "Pre-session UI bootstrap."),
    ("GET", "/api/v1/capabilities", "Pre-session UI bootstrap."),
    ("GET", "/api/v1/prekey/{peer}", "Public PQ prekey directory (GET only; POST is gated)."),
    ("POST", "/api/v1/inbox", "Federation S2S inbound; per-message envelope signature."),
    ("GET", "/api/v1/auth/challenge", "Credential bootstrap (device-key handshake)."),
    ("POST", "/api/v1/auth/session", "Credential bootstrap."),
    ("POST", "/api/v1/auth/enroll", "Credential bootstrap (operator-gated in-route)."),
    ("POST", "/api/v1/auth/enroll/open", "Credential bootstrap (operator-gated in-route)."),
)


def _compile_pattern(pattern: str) -> "re.Pattern[str]":
    """Compile a route pattern to a full-match regex, ``{param}`` -> one segment.

    ``{peer_id}`` (and any ``{...}`` placeholder) matches exactly one path segment
    (``[^/]+``), so one compiled pattern matches both a concrete request path and
    the FastAPI ``path_format`` string the coverage gate feeds it.
    """
    out = ["^"]
    for seg in re.split(r"(\{[^/}]+\})", pattern):
        if seg.startswith("{") and seg.endswith("}"):
            out.append(r"[^/]+")
        elif seg:
            out.append(re.escape(seg))
    out.append("$")
    return re.compile("".join(out))


_COMPILED_CAPABILITY: tuple[tuple[str, "re.Pattern[str]", str], ...] = tuple(
    (method.upper(), _compile_pattern(pattern), cap)
    for method, pattern, cap in _ROUTE_CAPABILITY_RULES
)
_COMPILED_SELF_AUTH: tuple[tuple[str, "re.Pattern[str]", str, str], ...] = tuple(
    (method.upper(), _compile_pattern(pattern), verifier, rationale)
    for method, pattern, verifier, rationale in _SELF_AUTH_RULES
)


def route_capability(method: str, path: str) -> Optional[str]:
    """Return the capability a gated ``(method, path)`` exercises, or None.

    Method-aware and full-match: the replacement for the suffix-only
    ``_capability_for_path``. Returns None for public routes, self-auth routes,
    and any unmapped route (in enforce that None fails closed; the coverage gate
    guarantees no legitimate gated route stays unmapped).
    """
    method = (method or "").upper()
    for m, rx, cap in _COMPILED_CAPABILITY:
        if m == method and rx.match(path):
            return cap
    return None


def is_self_auth_route(method: str, path: str) -> bool:
    """True iff ``(method, path)`` is a declared self-auth route (own verifier)."""
    method = (method or "").upper()
    return any(m == method and rx.match(path) for m, rx, _v, _r in _COMPILED_SELF_AUTH)


def authz_pdp_mode() -> str:
    """Return the authz PDP mode: 'off' (default), 'shadow', or 'enforce'.

    Read at call time so an operator can stage the rollout without a reimport.
    Anything unrecognized reads as 'off'.
    """
    mode = os.getenv(AUTHZ_PDP_FLAG, "").strip().lower()
    return mode if mode in ("shadow", "enforce") else "off"


def dataplane_auth_enabled() -> bool:
    """Return True iff the fail-closed data-plane CapAuth gate is switched on.

    Reads ``SKCHAT_DATAPLANE_AUTH`` at call time (not import time) so an
    operator editing the unit's ``Environment=`` line — or a test — can flip it
    without a reimport. Default OFF: absent / blank / anything not in the truthy
    set leaves the plane unauthenticated, exactly as before this gate existed.
    """
    return os.getenv(ENV_FLAG, "").strip().lower() in _TRUTHY


def accept_audience_tokens() -> bool:
    """Return True iff the audience-scoped-token credential path is switched on.

    Reads ``SKCHAT_ACCEPT_AUDIENCE_TOKENS`` at call time (like
    :func:`dataplane_auth_enabled`), default OFF. When off, the third credential
    path is never consulted and the validator behaves byte-identically to before
    this path existed.
    """
    return os.getenv(ACCEPT_AUDIENCE_ENV_FLAG, "").strip().lower() in _TRUTHY


def audience_mint_enabled() -> bool:
    """Return True iff the backend audience-token mint endpoint is switched on.

    Reads ``SKCHAT_AUDIENCE_MINT`` at call time (like :func:`dataplane_auth_enabled`),
    default OFF. When off, ``POST /api/v1/audience-token`` is inert (404) and never
    mints, so the app is byte-identical to before this endpoint existed.
    """
    return os.getenv(AUDIENCE_MINT_ENV_FLAG, "").strip().lower() in _TRUTHY


def issuer_shadow_enabled() -> bool:
    """Return True iff the server-side issuer shadow is switched on.

    Reads ``SKCHAT_ISSUER_SHADOW`` at call time (like :func:`dataplane_auth_enabled`),
    default OFF. When off, no synthetic twin is minted and no comparison runs, so the
    request path is byte-identical to before this observability existed.
    """
    return os.getenv(ISSUER_SHADOW_ENV_FLAG, "").strip().lower() in _TRUTHY


class CapAuthValidator:
    """Verify a capauth credential presented on a data-plane request.

    Thin delegate to the canonical capauth verifier, lazy-imported so this
    module loads even where capauth isn't installed (the same contract as
    ``spaces/federation/assertion.py``). ``validate`` returns True **only** for a
    credential capauth affirms; it **fails closed** (returns False) on a missing
    credential, a verification failure, or any error resolving the backend.

    Accepts an operator-session JWT (Bearer), a base64url-encoded {"claim", "sig"}
    OpenPGP FQID assertion, or (only when ``SKCHAT_ACCEPT_AUDIENCE_TOKENS`` is on) a
    capauth audience-scoped token minted for the ``skchat`` audience, tried in that
    order. The OpenPGP form is verified through :func:`assertion.verify_signed`; the
    audience token through :func:`capauth.verify_audience_token`.
    """

    def validate(self, token: str) -> bool:
        if not token:
            return False
        try:
            return _verify_capauth_credential(token)
        except Exception:
            # Fail closed: any verifier/parse/backend error denies the request.
            logger.info("capauth credential rejected", exc_info=True)
            return False


def _verify_capauth_credential(token: str) -> bool:
    """Verify a base64url ``{claim, sig}`` capauth assertion. Raises on failure.

    Delegates to the in-repo, capauth-backed ``assertion.verify_signed`` (which
    checks the signature, freshness and the FQID->pubkey pin, raising on any
    problem). Lazy-imported so importing this module never drags in capauth.
    """
    # Operator-session JWT (the app's Bearer credential). Try this first;
    # fall through to the OpenPGP assertion path for daemon/agent callers.
    try:
        from .operator_auth import OperatorAuthError, verify_operator_session

        verify_operator_session(token)
        return True
    except OperatorAuthError:
        pass
    except Exception:
        logger.debug("operator-session credential check errored, falling through", exc_info=True)

    import base64
    import json

    from .spaces.federation.assertion import verify_signed

    padded = token + "=" * (-len(token) % 4)
    signed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    if isinstance(signed, dict) and "claim" in signed and "sig" in signed:
        verify_signed(signed)  # raises on bad signature / stale / unknown key
        return True

    # Third credential path (flag-gated, default OFF): a capauth audience-scoped
    # token minted for the skchat audience. Inert unless SKCHAT_ACCEPT_AUDIENCE_TOKENS
    # is on, so when off this is byte-identical to the prior `return False`.
    if accept_audience_tokens() and _verify_skchat_audience_token(token):
        return True

    return False


def _verify_skchat_audience_token(token: str) -> bool:
    """Verify a capauth audience-scoped token for the ``skchat`` audience.

    Wire form: the base64url of ``capauth.export_token(token)`` JSON (whitespace-
    free so it rides in an ``Authorization`` / ``X-CapAuth-Token`` header). We
    reconstruct the ``SignedToken`` via :func:`capauth.import_token` and accept it
    ONLY when :func:`capauth.verify_audience_token` affirms it for the ``skchat``
    audience: a valid signature, a live (unexpired) token, AND ``audience ==
    "skchat"``. An unscoped (``audience=None``) or wrong-audience token is never
    accepted here. Fails closed on any parse/verify error (returns False; the
    caller's try/except also backstops).
    """
    import base64

    from capauth import import_token, verify_audience_token

    padded = token + "=" * (-len(token) % 4)
    token_json = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    signed = import_token(token_json)  # raises ValueError if not a capauth token
    return bool(verify_audience_token(signed, SKCHAT_AUDIENCE))


# --------------------------------------------------------------------------- #
# Injectable validator singleton
# --------------------------------------------------------------------------- #
_validator: Optional[CapAuthValidator] = None


def get_validator() -> CapAuthValidator:
    """Return the process validator, creating the default on first use."""
    global _validator
    if _validator is None:
        _validator = CapAuthValidator()
    return _validator


def set_validator(validator: Optional[CapAuthValidator]) -> None:
    """Override the process validator (tests inject a mock; ``None`` resets)."""
    global _validator
    _validator = validator


def _extract_credential(request: Request) -> Optional[str]:
    """Pull the capauth credential off a request, or None if absent.

    Accepts ``Authorization: CapAuth <token>`` / ``Bearer <token>`` (or a bare
    ``Authorization: <token>``) and falls back to the ``X-CapAuth-Token`` header.
    """
    auth = (request.headers.get("authorization") or "").strip()
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("capauth", "bearer"):
            return parts[1].strip() or None
        if len(parts) == 1:
            return parts[0].strip() or None
    return (request.headers.get("x-capauth-token") or "").strip() or None


def _capability_for_path(path: str) -> Optional[str]:
    """Back-compat, method-agnostic shim over :func:`route_capability` (deprecated).

    Enforcement now uses the method-aware :func:`route_capability`; this wrapper is
    retained so callers that only have a path still resolve the historically-mapped
    capability. It returns the capability of the first HTTP method that maps this
    path, preserving the pre-method-aware results for the original three endpoints
    (``/api/send`` -> send, ``/api/v1/inbox`` -> inbox via GET, ``/api/v1/prekey``
    -> prekey via POST).
    """
    for method in ("POST", "GET", "PUT", "DELETE"):
        cap = route_capability(method, path)
        if cap is not None:
            return cap
    return None


def operator_subject(device_fp: str) -> str:
    """The PDP subject id for an enrolled operator device.

    Single source of truth for the ``operator:<device_fp>`` subject string so the
    capability grant issued at enrollment (see :mod:`skchat.operator_grants`) and
    the subject :func:`_extract_subject` hands the PDP can never drift apart.
    """
    return f"operator:{device_fp}"


def _extract_subject(token: str) -> Optional[str]:
    """Best-effort authenticated subject for the PDP call.

    The FQID assertion carries the subject in its ``claim.fqid``; an operator
    session carries a ``device_fp``. Returns None when neither can be read. Never
    raises (a missing subject just means the PDP denies, which shadow only logs).
    """
    try:
        from .operator_auth import verify_operator_session

        session = verify_operator_session(token)
        return operator_subject(session.device_fp)
    except Exception:
        logger.debug("operator-session subject resolution failed", exc_info=True)
    # Audience-token branch (CR-3.4 P1): a valid skchat-audience capauth token
    # resolves to its payload subject -- ``operator:<device_fp>`` for the seat
    # (section 4), or the fqid for a daemon-self token, both of which the PDP grant
    # bundle already covers. Tried after the operator-session branch and before the
    # FQID-assertion branch, and gated on ``accept_audience_tokens()`` like the
    # validator's accept branch. Byte-identical to before for any non-audience
    # credential: ``import_token`` raises structurally on an HS256 JWT (two dots, not
    # base64url JSON) or an FQID assertion (``{claim, sig}`` has no
    # ``skcapstone_token`` envelope key), so this branch never shadows them.
    if accept_audience_tokens():
        try:
            import base64

            from capauth import import_token, verify_audience_token

            padded = token + "=" * (-len(token) % 4)
            token_json = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            signed = import_token(token_json)  # raises ValueError if not a capauth token
            if verify_audience_token(signed, SKCHAT_AUDIENCE):
                return signed.payload.subject
        except Exception:
            logger.debug("audience-token subject resolution failed", exc_info=True)
    try:
        import base64
        import json

        padded = token + "=" * (-len(token) % 4)
        signed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        claim = signed.get("claim") if isinstance(signed, dict) else None
        if isinstance(claim, dict):
            return claim.get("fqid")
    except Exception:
        logger.debug("fqid subject resolution failed", exc_info=True)
    return None


def _pdp_allows(subject: Optional[str], capability: str, request: Request) -> Optional[bool]:
    """Run capauth.authz.decide for this request. None on any error (fail-safe).

    Deliberately does no authentication (that already happened): it decides from
    the stored enrollment/token facts about the already-authenticated subject.
    """
    if not subject:
        return False
    try:
        from capauth.authz import decide

        decision = decide(subject, capability, resource={"path": request.url.path})
        return bool(decision.allow)
    except Exception:
        logger.debug("authz PDP decide errored", exc_info=True)
        return None


def _redact(subject: Optional[str]) -> str:
    if not subject:
        return "?"
    return subject if len(subject) <= 8 else subject[:6] + "..."


def request_is_authenticated(request: Request) -> bool:
    """Return True iff the request carries a capauth credential the validator affirms.

    Unlike :func:`enforce_dataplane_auth` (a no-op when ``SKCHAT_DATAPLANE_AUTH`` is
    off), this ALWAYS consults the validator: it is for routes that must require
    authentication on their own terms regardless of the plane-wide gate flag (e.g.
    the audience-token mint endpoint, which must never mint for an anonymous caller
    even when the dataplane gate is off). Reuses :func:`_extract_credential` and the
    injectable :func:`get_validator`, so it fails closed on a missing/invalid
    credential and tests can stub the validator.
    """
    token = _extract_credential(request)
    return bool(token) and get_validator().validate(token)


def _credential_is_audience_token(token: str) -> bool:
    """True iff ``token`` is (structurally) a capauth audience-scoped token.

    Decodes the credential wire form and checks it is a capauth token whose
    ``audience`` is set (non-None). Purely structural -- it does NOT verify the
    signature (the caller has already run the validator for that); it only
    classifies the credential TYPE so the mint gate can refuse an audience token.
    Fails closed to False on any parse error (an HS256 JWT / FQID assertion / garbage
    is not an audience token).
    """
    try:
        import base64

        from capauth import import_token

        padded = token + "=" * (-len(token) % 4)
        token_json = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        signed = import_token(token_json)  # raises ValueError if not a capauth token
        return signed.payload.audience is not None
    except Exception:
        return False


def request_is_primary_authenticated(request: Request) -> bool:
    """Return True iff the request carries a PRIMARY capauth credential.

    A PRIMARY credential is an operator-session JWT or a signed FQID assertion --
    NOT an audience token. This is the anti-renewal-laundering rule (CR-3.4 P3): the
    token-mint routes must never mint FROM an audience token, or a short-lived token
    could re-mint itself forever and a leaked audience token would become effectively
    non-expiring.

    Implemented as ``request_is_authenticated`` (any accepted credential, via the
    injectable validator) MINUS the audience-token class, which is exactly
    {operator-session JWT, signed FQID assertion}. Keeping the injectable-validator
    seam means the existing mint-route tests (which stub the validator) are
    unaffected, while a real audience token -- affirmed by the validator -- is refused
    here.
    """
    token = _extract_credential(request)
    if not token or not get_validator().validate(token):
        return False
    # An audience token authenticates but is NOT primary: refuse it at the mint gate.
    if _credential_is_audience_token(token):
        return False
    return True


# --------------------------------------------------------------------------- #
# Server-side issuer shadow (CR-3.4 P5 / Phase 1): prove the audience path would
# resolve the SAME subject and produce the SAME PDP decision as the live HS256
# operator session, on real traffic, WITHOUT touching the response. Pure
# observation, gated by SKCHAT_ISSUER_SHADOW (default OFF).
# --------------------------------------------------------------------------- #
#: Per-device-fingerprint cache of the synthetic audience twin wire form:
#: ``device_fp -> (wire, exp_epoch)``. Refreshed 5 minutes before expiry so PGP
#: signing is not paid per request (R8).
_shadow_twins: dict[str, tuple[str, float]] = {}
_shadow_lock = threading.Lock()
_shadow_ok_count = 0
_SHADOW_REFRESH_SKEW = 300  # re-mint the twin 5 min before it expires


def _get_shadow_twin(device_fp: str) -> Optional[str]:
    """Return the cached (or freshly minted) audience-twin wire for ``device_fp``.

    Minting is done outside the lock (PGP signing is slow); the cache holds the
    exported wire form and its expiry. Returns None if minting fails.
    """
    import time

    now = time.time()
    with _shadow_lock:
        cached = _shadow_twins.get(device_fp)
        if cached is not None and cached[1] - _SHADOW_REFRESH_SKEW > now:
            return cached[0]

    from .operator_audience import mint_operator_audience_token, wire_form

    token = mint_operator_audience_token(device_fp)
    wire = wire_form(token)
    exp = token.payload.expires_at
    exp_epoch = exp.timestamp() if hasattr(exp, "timestamp") else now + 3600
    with _shadow_lock:
        _shadow_twins[device_fp] = (wire, exp_epoch)
    return wire


def _record_shadow_ok() -> None:
    """Count one converged shadow comparison and emit a periodic heartbeat.

    Logged at WARNING, not INFO: the webui runs uvicorn at ``log_level="warning"``,
    so an INFO heartbeat never reaches the journal and the Phase-2 soak gate
    ("nonzero issuer-shadow ok heartbeats; silence never passes") could never be
    observed. Fires on the 1st ok and every 100th thereafter, so the line volume
    stays low while still proving the shadow is alive rather than merely silent.
    """
    global _shadow_ok_count
    _shadow_ok_count += 1
    if _shadow_ok_count % 100 == 1:
        logger.warning("issuer-shadow ok count=%s", _shadow_ok_count)


def _issuer_shadow_compare(request: Request, token: str) -> None:
    """Compare the HS256 operator session against its synthetic audience twin.

    Runs ONLY for a request whose credential verifies as an HS256 operator session.
    Mints/reuses the per-fingerprint audience twin, runs the full audience accept
    path on it (wire decode, import, verify_audience_token, revocation via P2), and
    compares (a) the resolved subject and (b) the PDP decision for the route. Logs one
    structured divergence line on ANY mismatch, else increments a heartbeat. NEVER
    raises into the request path and NEVER changes the response.
    """
    try:
        from .operator_auth import verify_operator_session

        try:
            session = verify_operator_session(token)
        except Exception:
            return  # not an HS256 operator session; the shadow only compares those

        hs_subject = operator_subject(session.device_fp)

        twin_wire = _get_shadow_twin(session.device_fp)
        aud_authenticated = bool(twin_wire) and _verify_skchat_audience_token(twin_wire)
        aud_subject = _extract_subject(twin_wire) if aud_authenticated else None

        method = request.method
        path = request.url.path
        capability = route_capability(method, path)
        hs_decision: Optional[bool] = None
        aud_decision: Optional[bool] = None
        if capability is not None:
            hs_decision = _pdp_allows(hs_subject, capability, request)
            aud_decision = _pdp_allows(aud_subject, capability, request)

        subject_diverges = aud_subject != hs_subject
        decision_diverges = capability is not None and hs_decision != aud_decision

        if not aud_authenticated or subject_diverges or decision_diverges:
            logger.warning(
                "issuer-shadow divergence path=%s cap=%s hs_subject=%s aud_subject=%s "
                "hs_decision=%s aud_decision=%s aud_authenticated=%s",
                path,
                capability,
                _redact(hs_subject),
                _redact(aud_subject),
                hs_decision,
                aud_decision,
                aud_authenticated,
            )
        else:
            _record_shadow_ok()
    except Exception:
        # Observation only: any error here must never affect the request.
        logger.debug("issuer-shadow compare errored (non-fatal)", exc_info=True)


def _stash_operator_session(request: Request, token: str) -> None:
    """Record the verified operator session on ``request.state`` for routes.

    The gate verifies the credential and then throws the result away, so a route
    that needs to know WHICH device authenticated it (the prekey publish, which
    must attribute the slot to a device) had no way to find out. Stashing it here
    keeps that knowledge on the one code path that already proved it.

    Best-effort: a non-operator credential (guest/peer/audience token) simply
    leaves the attribute unset, and callers treat that as "unknown device".
    """
    try:
        from .operator_auth import verify_operator_session

        request.state.operator_session = verify_operator_session(token)
        from .device_registry import touch_throttled

        touch_throttled(request.state.operator_session.device_fp)
    except Exception:
        # Not an operator session (or an unverifiable one). Nothing to stash.
        pass


def enforce_dataplane_auth(request: Request) -> None:
    """Fail-closed CapAuth gate for a single data-plane request.

    No-op when the gate flag is off. When on, authentication runs exactly as
    before (a missing or invalid credential raises 401). The authz PDP then layers
    on per ``SKCHAT_AUTHZ_PDP``: 'off' authenticates only; 'shadow' also computes
    the PDP decision, logs any divergence from the legacy allow, and returns the
    legacy outcome (no behavior change); 'enforce' additionally requires the PDP
    to allow (403 on deny).
    """
    if not dataplane_auth_enabled():
        return
    token = _extract_credential(request)
    legacy_ok = bool(token) and get_validator().validate(token)

    if legacy_ok and token:
        _stash_operator_session(request, token)

    mode = authz_pdp_mode()

    # Issuer shadow (CR-3.4 P5, default OFF): for an authenticated request, compare
    # the audience path against the live HS256 session on a synthetic twin. Pure
    # observation -- never alters legacy_ok, control flow, or the response.
    if legacy_ok and token and issuer_shadow_enabled():
        _issuer_shadow_compare(request, token)

    if mode == "off":
        if not legacy_ok:
            raise HTTPException(status_code=401, detail="capauth authentication required")
        return

    # Shadow / enforce: authentication must still pass first.
    if not legacy_ok:
        raise HTTPException(status_code=401, detail="capauth authentication required")

    method = request.method
    path = request.url.path

    # Self-auth routes (token mints) authenticate on their own declared terms and
    # are never routed through the PDP (L1.1 step 1 / L1.4). Legacy auth already
    # passed above (defense in depth); the route's own verifier governs the rest.
    # Without this, a self-auth route would 403 under enforce for having no
    # capability, breaking the audience/embed token mints.
    if is_self_auth_route(method, path):
        return

    capability = route_capability(method, path)
    pdp_allow: Optional[bool] = None
    if capability is not None:
        pdp_allow = _pdp_allows(_extract_subject(token), capability, request)

    if mode == "shadow":
        # Measure only: log divergence, return the legacy outcome unchanged.
        if capability is None:
            # L1.3: shadow MUST be able to SEE the unmapped-gated class -- the exact
            # structural blind spot that hid incident (b) until the enforce flip.
            # One log line turns that enforce surprise into a soak observable.
            logger.warning(
                "authz PDP unmapped-route method=%s path=%s subject=%s",
                method,
                path,
                _redact(_extract_subject(token)),
            )
        elif pdp_allow is not None and pdp_allow != legacy_ok:
            logger.warning(
                "authz PDP divergence path=%s cap=%s subject=%s legacy=%s pdp=%s",
                path,
                capability,
                _redact(_extract_subject(token)),
                legacy_ok,
                pdp_allow,
            )
        return

    # enforce: the PDP governs. Unknown capability or a decide() error fails closed.
    if capability is None or not pdp_allow:
        # We only reach here having passed authentication (legacy allow), so ANY
        # PDP deny in enforce is a divergence from the legacy outcome. Log it as the
        # enforce-deny signal (the rollback guard watches this line to auto-revert to
        # shadow if the enforce flip starts 403ing legitimate traffic), then fail closed.
        logger.warning(
            "authz PDP enforce-deny path=%s cap=%s subject=%s pdp=%s",
            request.url.path,
            capability,
            _redact(_extract_subject(token)),
            pdp_allow,
        )
        raise HTTPException(status_code=403, detail="capauth authorization denied")


async def require_dataplane_auth(request: Request) -> None:
    """FastAPI dependency form of :func:`enforce_dataplane_auth`.

    Wire onto a protected route with ``Depends(require_dataplane_auth)``.
    """
    enforce_dataplane_auth(request)
