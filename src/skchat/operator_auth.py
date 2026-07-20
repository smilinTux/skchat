"""Operator-tier device-key auth: challenge-response to an HS256 session JWT.

Models guest_groups.mint_guest_session / verify_guest_session but with its own
tier ("operator-session") and its own signing secret so operator and guest
tokens never share a key. Ships dark: nothing calls this until the middleware
gate is enabled in the final rollout task.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import jwt  # PyJWT, already a dependency (see guest_groups.py)
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

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


_CHALLENGE_TTL = 120
_challenges: dict[str, int] = {}
_clock = threading.Lock()


def device_fingerprint(device_pubkey_b64: str) -> str:
    return hashlib.sha256(device_pubkey_b64.encode()).hexdigest()[:16]


def issue_challenge() -> tuple[str, int]:
    nonce = secrets.token_urlsafe(24)
    exp = int(time.time()) + _CHALLENGE_TTL
    with _clock:
        # opportunistic sweep of expired nonces
        now = int(time.time())
        for k in [k for k, v in _challenges.items() if v < now]:
            _challenges.pop(k, None)
        _challenges[nonce] = exp
    return nonce, exp


def consume_challenge(nonce: str) -> bool:
    with _clock:
        exp = _challenges.pop(nonce, None)
    return exp is not None and exp >= int(time.time())


def verify_device_signature(
    *, device_pubkey_b64: str, payload: bytes, sig_b64: str
) -> bool:
    try:
        spki = base64.b64decode(device_pubkey_b64)
        pub = serialization.load_der_public_key(spki)
        raw = base64.b64decode(sig_b64)
        if len(raw) == 64:  # WebCrypto P1363 r||s
            r = int.from_bytes(raw[:32], "big")
            s = int.from_bytes(raw[32:], "big")
            der = encode_dss_signature(r, s)
        else:  # already DER
            der = raw
        pub.verify(der, payload, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class DeviceStore:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text() or "{}")

    def enroll(self, device_pubkey_b64: str) -> str:
        fp = device_fingerprint(device_pubkey_b64)
        with self._lock:
            self._data[fp] = device_pubkey_b64
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: temp file in the same directory, then os.replace()
            # onto the target, so a crash mid-write never leaves a torn file
            # (either the old contents are intact or the new ones are, never
            # a half-written mix).
            tmp = self._path.with_suffix(self._path.suffix + f".tmp-{os.getpid()}")
            tmp.write_text(json.dumps(self._data))
            os.replace(tmp, self._path)
        return fp

    def is_enrolled(self, device_fp: str) -> bool:
        return device_fp in self._data

    def pubkey_for(self, device_fp: str) -> str | None:
        return self._data.get(device_fp)
