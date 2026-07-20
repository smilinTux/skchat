"""Operator-tier device-key auth: challenge-response to an HS256 session JWT.

Models guest_groups.mint_guest_session / verify_guest_session but with its own
tier ("operator-session") and its own signing secret so operator and guest
tokens never share a key. Ships dark: nothing calls this until the middleware
gate is enabled in the final rollout task.
"""
from __future__ import annotations
import os, time, uuid, secrets
from dataclasses import dataclass
import jwt  # PyJWT, already a dependency (see guest_groups.py)

_TIER = "operator-session"
_DEFAULT_TTL = 12 * 3600
_MAX_TTL = 24 * 3600


class OperatorAuthError(Exception):
    pass


@dataclass
class OperatorSession:
    jti: str
    device_fp: str
    exp: int


def _secret() -> str:
    s = os.environ.get("SKCHAT_OPERATOR_TOKEN_SECRET", "")
    if not s:
        raise OperatorAuthError("SKCHAT_OPERATOR_TOKEN_SECRET not set")
    return s


def mint_operator_session(*, device_fp: str, ttl: int | None = None) -> str:
    now = int(time.time())
    ttl = _DEFAULT_TTL if ttl is None else min(ttl, _MAX_TTL)
    claims = {
        "jti": uuid.uuid4().hex,
        "tier": _TIER,
        "device_fp": device_fp,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(claims, _secret(), algorithm="HS256")


def verify_operator_session(token: str) -> OperatorSession:
    try:
        claims = jwt.decode(
            token, _secret(), algorithms=["HS256"],
            options={"require": ["jti", "tier", "device_fp", "iat", "exp"]},
        )
    except jwt.PyJWTError as e:
        raise OperatorAuthError(f"invalid operator session: {e}") from e
    if claims.get("tier") != _TIER:
        raise OperatorAuthError("wrong tier")
    from .guest import _is_revoked  # reuse the guest revocation set
    if _is_revoked(claims["jti"]):
        raise OperatorAuthError("revoked")
    return OperatorSession(jti=claims["jti"], device_fp=claims["device_fp"], exp=claims["exp"])
