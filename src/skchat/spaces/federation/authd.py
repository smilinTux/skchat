"""sk-lk-authd orchestration (spec §7): verify signed assertion -> trust policy
-> mint a LiveKit JWT with the LOCAL SFU secret. Seams injectable for tests."""

from __future__ import annotations

from typing import Callable

from skchat.spaces.federation.assertion import Assertion, verify_signed
from skchat.spaces.federation.trust import AccessLevel, TrustPolicy
from skchat.spaces.roles import Role


class AuthDenied(Exception):
    pass


def _default_mint(identity: str, role: Role, space_id: str) -> str:
    from skchat.spaces.tokens import mint_space_token
    return mint_space_token(identity, identity.split("@")[0], role, space_id, 3600)


_ROLE_FOR = {AccessLevel.FULL: Role.SPEAKER, AccessLevel.SUBSCRIBE: Role.LISTENER}


def authorize(
    signed: dict,
    *,
    sfu_ws_url: str,
    _verify: Callable[..., Assertion] = verify_signed,
    _access: Callable[[str], AccessLevel] | None = None,
    _mint: Callable[..., str] | None = None,
) -> dict:
    assertion = _verify(signed)
    access = (_access or TrustPolicy().access_for)(assertion.fqid)
    if access == AccessLevel.DENY:
        raise AuthDenied(f"fqid {assertion.fqid!r} not permitted")
    role = _ROLE_FOR[access]
    mint = _mint or _default_mint
    token = mint(assertion.fqid, role, assertion.space_id)
    return {"sfu_ws_url": sfu_ws_url, "token": token, "role": role.value,
            "identity": assertion.fqid, "space_id": assertion.space_id}
