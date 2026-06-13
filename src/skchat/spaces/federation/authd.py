"""sk-lk-authd orchestration (spec §7): verify signed assertion -> trust policy
-> mint a LiveKit JWT with the LOCAL SFU secret. Seams injectable for tests."""

from __future__ import annotations

from typing import Callable

from skchat.spaces.federation.assertion import Assertion, verify_signed
from skchat.spaces.federation.nonce import NonceCache
from skchat.spaces.federation.trust import AccessLevel, TrustPolicy
from skchat.spaces.roles import Role

# Replay window — kept in lockstep with the assertion freshness `max_age` so a
# nonce can't outlive the assertion that carried it.
MAX_AGE = 300

# Process-wide replay cache (single-replica). A multi-replica authd must swap
# this for a shared store — see nonce.py.
_NONCE = NonceCache()


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
    _nonce: NonceCache = _NONCE,
    _remote_max_role: str | None = None,
    _space_live: Callable[[str], bool] | None = None,
) -> dict:
    assertion = _verify(signed)
    # I1: reject replayed assertions (same fqid+nonce within the freshness
    # window) BEFORE minting any token.
    if not _nonce.check_and_add(assertion.fqid, assertion.nonce, MAX_AGE):
        raise AuthDenied("replay detected")
    # space validation: if a checker is provided, the space must be live
    if _space_live is not None and not _space_live(assertion.space_id):
        raise AuthDenied(f"unknown or ended space {assertion.space_id!r}")
    access = (_access or TrustPolicy().access_for)(assertion.fqid)
    if access == AccessLevel.DENY:
        raise AuthDenied(f"fqid {assertion.fqid!r} not permitted")
    role = _ROLE_FOR[access]
    # remote-role cap: an operator can cap FULL-trust remotes at listener
    rmr = _remote_max_role if _remote_max_role is not None else TrustPolicy().remote_max_role
    if role == Role.SPEAKER and rmr == "listener":
        role = Role.LISTENER
    mint = _mint or _default_mint
    token = mint(assertion.fqid, role, assertion.space_id)
    return {"sfu_ws_url": sfu_ws_url, "token": token, "role": role.value,
            "identity": assertion.fqid, "space_id": assertion.space_id}
