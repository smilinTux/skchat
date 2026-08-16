"""FastAPI routes for the operator device-key auth handshake.

Ships dark: these routes exist but nothing is gated on their output until the
enforcement middleware is added in a later task. Enrollment is operator-gated
(loopback/tailnet or SKCHAT_GUEST_OPERATOR_TOKEN) via the existing
guest._require_operator; challenge/session are open by design, they only mint
a session for a device whose key is already enrolled and the request must
carry a valid device signature over the canonical challenge payload.
"""

from __future__ import annotations

import base64
import json
import logging

from fastapi import APIRouter, FastAPI, HTTPException, Request

from . import operator_auth as oa
from .guest import _require_operator
from .operator_grants import grant_operator_capabilities_detailed
from .pairing_gate import PairingGate

_pairing = PairingGate(max_accepts_per_window=1)  # operator enroll: 1 device per window


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


#: Browser needles, MOST specific first. Order is load-bearing: an Edge UA also
#: contains "Chrome" and "Safari", and a Chrome UA also contains "Safari", so a
#: naive scan mislabels both.
_UA_BROWSERS: tuple[tuple[str, str], ...] = (
    ("edg/", "Edge"),
    ("firefox", "Firefox"),
    ("chrome", "Chrome"),
    ("safari", "Safari"),
)

#: OS needles, MOST specific first, mapping to ``(display, platform)``. Order is
#: load-bearing here too: an Android UA also contains "Linux", and an iOS UA also
#: contains "like Mac OS X".
_UA_SYSTEMS: tuple[tuple[str, str, str], ...] = (
    ("android", "Android", "android"),
    ("iphone", "iOS", "ios"),
    ("ipad", "iOS", "ios"),
    ("cros", "ChromeOS", "chromeos"),
    ("windows", "Windows", "windows"),
    ("macintosh", "macOS", "macos"),
    ("mac os x", "macOS", "macos"),
    ("linux", "Linux", "linux"),
)


def _require_operator_or_link_code(request: Request) -> None:
    """Authorize opening an enrollment window, or raise 401/403.

    Accepts everything :func:`skchat.guest._require_operator` accepts, plus a
    live single-use device link code presented in the SAME header. That header
    reuse is deliberate: the app already has a paste field wired to it, so a
    short-lived code drops into the existing linking flow with no client change.

    Scoped to THIS route on purpose. ``_require_operator`` also guards guest
    invites, prekey signing and the call routes; a code that opened those would
    be a strictly worse operator token rather than a better one. Here it only
    opens an enrollment window, and the device still has to sign the window
    nonce with its own key, and (Phase 3) still lands pending approval.

    The code is checked FIRST and burns on use, so presenting one always spends
    it even on a caller who would have passed on loopback anyway. That is the
    honest reading of intent: someone who typed a code meant to use it.
    """
    from fastapi import HTTPException

    from skchat import link_codes as LC

    headers = getattr(request, "headers", {}) or {}
    presented = (headers.get("x-operator-token") or "").strip()
    if not presented:
        auth = (headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
    if presented and LC.verify(presented):
        return
    try:
        _require_operator(request)
    except HTTPException:
        raise


def _derive_label(user_agent: str) -> tuple[str, str]:
    """Best-effort ``(label, platform)`` from a User-Agent string.

    Used when the client sent no label of its own, which is the COMMON case and
    not an edge case: the Flutter web build has no ``dart:io``, so it cannot read
    a hostname and correctly sends no label at all. Every browser device
    therefore lands here.

    That makes this the only thing keeping the operator's device list readable,
    so it names the operating system as well as the browser. Naming only the
    browser is what the first live cutover did, and it produced two rows both
    reading "Chrome": a phone and a Linux desktop, indistinguishable. Telling
    devices apart is the entire point of the feature.

    A native client is still named "App device" with no invented OS: its UA
    carries neither, which is exactly why R2 has such a client send its own
    signed label.
    """
    ua = (user_agent or "").strip()
    if not ua:
        return "Unknown device", "unknown"
    lowered = ua.lower()

    browser = next((name for needle, name in _UA_BROWSERS if needle in lowered), "")
    system = next(
        ((display, platform) for needle, display, platform in _UA_SYSTEMS if needle in lowered),
        None,
    )

    if browser and system:
        return f"{browser} on {system[0]}", system[1]
    if system:
        # An unrecognised browser on a known OS: the OS alone still tells the
        # operator which physical device this is, which is what they need.
        return system[0], system[1]
    if browser:
        return browser, "web"
    if "dart" in lowered:
        return "App device", "app"
    return ua[:40], "unknown"


def _claim_bootstrap_window(device_fp: str) -> bool:
    """Auto-approve this device if a reset-opened bootstrap window is standing.

    Right after ``skchat devices reset`` there are no approved devices, so there
    is nobody to approve the first one from. The reset opens a short single-use
    window instead of sending the operator back to the terminal.

    This is not a hole in approval-to-link: opening the window requires a command
    ON THE BOX, which is stronger evidence than holding the (long-lived,
    plaintext) operator token, and it is bounded and single-use. Best-effort, so
    a window problem can never break an enrollment.
    """
    try:
        from skchat import bootstrap_window as BW
        from skchat import device_registry as DR

        if not BW.consume():
            return False
        DR.set_approved(device_fp, True)
        logging.getLogger("skchat.operator_auth_routes").info(
            "device %s auto-approved via the bootstrap window", device_fp
        )
        return True
    except Exception:  # pragma: no cover - never break enrollment
        logging.getLogger("skchat.operator_auth_routes").debug(
            "bootstrap window claim failed (best-effort)", exc_info=True
        )
        return False


def _record_enrollment(request: Request, device_fp: str, *, label: object) -> None:
    """Write the registry row for a freshly enrolled device. Best-effort.

    A re-enroll also clears any prior revocation: re-linking a device you
    previously unlinked is how you bring it back.
    """
    try:
        from skchat import device_registry as DR
        from skchat.guest import unrevoke_device

        user_agent = (request.headers.get("user-agent") or "").strip()
        if isinstance(label, str) and label.strip():
            text, source = label.strip()[:64], "client"
            _derived, platform = _derive_label(user_agent)
        else:
            text, platform = _derive_label(user_agent)
            source = "derived"
        DR.record_enroll(
            device_fp,
            label=text,
            label_source=source,
            platform=platform,
            user_agent=user_agent,
        )
        unrevoke_device(device_fp)
    except Exception:  # pragma: no cover - never break enrollment
        logging.getLogger("skchat.operator_auth_routes").debug(
            "enrollment registry record failed (best-effort)", exc_info=True
        )


def register_operator_auth_routes(app: FastAPI, *, device_store: oa.DeviceStore) -> None:
    router = APIRouter(prefix="/api/v1/auth")

    @router.post("/enroll/open")
    async def enroll_open(request: Request):
        _require_operator_or_link_code(request)
        window = _pairing.open_window()
        out = {"window_nonce": window["nonce"], "exp": window["expires_at"]}

        # capauth card N10: a `verified` enrollment must carry the device's
        # signature over capauth's own domain-separated challenge. Those bytes
        # are a pure function of the device's PUBLIC key, so a client that sends
        # its pubkey here gets them back with no extra round trip and nothing
        # secret disclosed. Deriving them SERVER-side (from capauth's exported
        # helpers) rather than making the client re-implement capauth's
        # fingerprint and subject canonicalization is the point: a client that
        # hardcoded either would silently start producing rejected proofs, and
        # the failure mode of a rejected proof is a quiet tier downgrade.
        #
        # Optional and additive: the shipped web build POSTs no body at all, so
        # a missing/blank/unparseable body must return the original two-key
        # response rather than 400.
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - no body is the pre-existing contract
            body = None
        pub = (body or {}).get("device_pubkey") if isinstance(body, dict) else None
        if isinstance(pub, str) and pub.strip():
            try:
                from .operator_grants import verified_enrollment_challenge

                # Derive from the key EXACTLY as presented, never a stripped
                # copy: /enroll fingerprints the raw string it is sent, so
                # challenging a normalized variant would hand back bytes whose
                # signature that enroll then rejects, i.e. the silent tier
                # downgrade this whole change exists to remove.
                out["capauth_challenge"] = base64.b64encode(
                    verified_enrollment_challenge(pub)
                ).decode()
            except Exception:  # pragma: no cover - never block opening a window
                logging.getLogger("skchat.operator_auth_routes").warning(
                    "could not derive the capauth enrollment challenge; this device "
                    "will be unable to enroll 'verified' and will fall back to 'tofu'",
                    exc_info=True,
                )
        return out

    @router.post("/enroll")
    async def enroll(request: Request):
        body = await request.json()
        pub = body.get("device_pubkey")
        wnonce = body.get("window_nonce")
        sig = body.get("sig")
        if not (pub and wnonce and sig):
            raise HTTPException(400, "device_pubkey, window_nonce, sig required")
        ok, _reason = _pairing.check(wnonce)
        if not ok:
            raise HTTPException(401, "enrollment window closed or invalid")
        # R2: when the client names the device, that name is part of what it
        # signs, so it cannot be rewritten in transit. A label-less enroll keeps
        # verifying the original two-field payload, so the shipped web build
        # (which signs only nonce + pubkey) is not locked out.
        label = body.get("label")
        signed_claims = {"nonce": wnonce, "device_pubkey": pub}
        if isinstance(label, str) and label.strip():
            signed_claims["label"] = label.strip()[:64]
        if not oa.verify_device_signature(
            device_pubkey_b64=pub,
            payload=_canon(signed_claims),
            sig_b64=sig,
        ):
            raise HTTPException(401, "device signature invalid")
        _pairing.consume()
        device_fp = device_store.enroll(pub)
        # Grant the enrolled device its skchat capabilities so a POST
        # /api/v1/prekey from its session is AUTHORIZED (not just authenticated)
        # when the authz PDP is enforcing. Best-effort: a grant failure is logged
        # inside and never breaks the enrollment response.
        #
        # `capauth_proof` is the device's signature over the `capauth_challenge`
        # handed back by /enroll/open (capauth card N10). It is a SEPARATE
        # signature from `sig` above and cannot be derived from it: `sig` covers
        # skchat's own {nonce, device_pubkey} payload, while capauth requires its
        # own domain-separated challenge bytes, deliberately so that a signature
        # made for one purpose can never be replayed as proof for another. Both
        # come from the same device key, so verifying `sig` first means an
        # unauthenticated caller never reaches the grant.
        #
        # A client that does not send one is NOT locked out: the grant enrolls it
        # at `tofu`, the tier it can actually prove, and logs the downgrade with
        # the capabilities it costs. The mode reached is reported below so the
        # degradation is visible to the client instead of surfacing later as an
        # unexplained PDP denial.
        outcome = grant_operator_capabilities_detailed(
            device_fp, pub, capauth_proof=body.get("capauth_proof")
        )
        _record_enrollment(request, device_fp, label=body.get("label"))
        _claim_bootstrap_window(device_fp)
        # Tell the caller whether it is pending approval (Phase 3): a missing
        # registry row (recording failed, best-effort) reads as approved, same
        # as everywhere else this is checked, so a registry hiccup never tells
        # an otherwise-fine device it is stuck pending.
        from . import device_registry as DR

        row = DR.get_device(device_fp)
        approved = True if row is None else DR.is_approved(row)
        return {
            "device_fp": device_fp,
            "approved": approved,
            # The enrollment mode ACTUALLY recorded, never the one requested.
            "enrollment_mode": outcome.mode,
            # Present only when the intended tier was not reached, so a client
            # (and anyone reading a capture) can tell a degraded link from a full
            # one without waiting for a capability to mysteriously 403.
            **({"enrollment_downgrade_reason": outcome.reason} if outcome.reason else {}),
        }

    @router.get("/challenge")
    async def challenge():
        nonce, exp = oa.issue_challenge()
        return {"nonce": nonce, "exp": exp}

    @router.post("/session")
    async def session(request: Request):
        body = await request.json()
        fp = body.get("device_fp")
        nonce = body.get("nonce")
        sig = body.get("sig")
        if not (fp and nonce and sig):
            raise HTTPException(400, "device_fp, nonce, sig required")
        if not oa.consume_challenge(nonce):
            raise HTTPException(401, "challenge nonce invalid or expired")
        pub = device_store.pubkey_for(fp)
        if not pub:
            raise HTTPException(401, "device not enrolled")
        if not oa.verify_device_signature(
            device_pubkey_b64=pub,
            payload=_canon({"nonce": nonce, "device_fp": fp}),
            sig_b64=sig,
        ):
            raise HTTPException(401, "challenge signature invalid")
        token = oa.mint_operator_session(device_fp=fp)
        # A pending (not-yet-approved) device cannot mint a usable session: the
        # token above is a valid JWT, but the immediate self-verify below is
        # exactly what every OTHER route would do to it, so failing here means
        # this route never hands back a token the device could not actually
        # use for anything. See device_registry.is_approved and the Phase 3
        # (approval-to-link) section of the device management design.
        try:
            sess = oa.verify_operator_session(token)
        except oa.OperatorAuthError as exc:
            raise HTTPException(403, str(exc)) from exc
        resp = {"session_token": token, "expires_at": sess.exp}
        # CR-3.4 AC1 / Phase 1: ALSO mint a parallel capauth audience token for
        # operator:<device_fp>, gated by SKCHAT_OPERATOR_AUDIENCE_ISSUE (default
        # OFF) and NON-FATAL. The HS256 session above stays the primary credential
        # the client uses; this is additive so today's clients (which read only
        # session_token) are unaffected. A mint failure returns None and never
        # breaks the handshake -- the seat cannot be locked out by the audience path.
        from .operator_audience import issue_operator_audience, operator_issuer_policy

        extra = issue_operator_audience(fp)
        if extra:
            resp.update(extra)
        # CR-3.4 PR4 / P6: echo the seat's issuer policy so the client's choice
        # of credential is server-driven and reversible with no app rebuild
        # (hs256 default / prefer-audience / audience-only). Always present.
        resp["issuer_policy"] = operator_issuer_policy()
        return resp

    app.include_router(router)
