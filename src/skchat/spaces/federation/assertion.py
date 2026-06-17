"""Signed FQID assertion (spec §7) — the OpenID-token analog.

A client builds + signs an Assertion with its capauth key; a (possibly remote)
sk-lk-authd verifies it. Crypto is injectable: `sign`/`verify` default to the
capauth PGP backend, `resolve_pubkey` to skcomms' FQID->pubkey loader.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional


class AssertionError(Exception):
    pass


@dataclass
class Assertion:
    fqid: str
    space_id: str
    issued_at: int
    nonce: str


def _canonical(a: Assertion) -> bytes:
    return json.dumps(asdict(a), sort_keys=True, separators=(",", ":")).encode()


def _default_sign(payload: bytes) -> str:
    # NOTE: lazy imports — only exercised in production, not under test.
    # Verified import paths 2026-06-13: capauth.resolve_agent_identity and
    # capauth.crypto.get_backend both resolve.
    from capauth import resolve_agent_identity
    from capauth.crypto import get_backend

    ident = resolve_agent_identity()
    # private key armor + passphrase resolved from the agent's capauth dir
    from pathlib import Path

    base = Path.home() / ".skcapstone" / "agents" / ident.agent / "capauth" / "identity"
    priv = (base / "private.asc").read_text()
    passphrase = ""  # agent keys are passphrase-less in this deployment
    return get_backend().sign(payload, priv, passphrase)


def _default_verify(payload: bytes, sig: str, pub: str) -> bool:
    from capauth.crypto import get_backend

    return get_backend().verify(payload, sig, pub)


def _default_resolve_pubkey(fqid: str) -> Optional[str]:
    # NOTE: federation verification requires a REALM-qualified pinned key. We
    # resolve the pubkey from a TOFU/directory pin keyed on the FULL fqid
    # (agent@host.realm). The bare-agent skcomms resolver
    # (skcomms.mailbox._load_verifier_key) is intentionally NOT used here: it
    # discards the realm, so `lumina@chef.skworld` and `lumina@evil.attacker`
    # would collide → impersonation (S5 review C1).
    from skchat.spaces.federation.keystore import federation_pubkey

    return federation_pubkey(fqid)


def build_signed(a: Assertion, *, sign: Callable[[bytes], str] = _default_sign) -> dict:
    payload = _canonical(a)
    return {"claim": payload.decode(), "sig": sign(payload)}


def verify_signed(
    signed: dict,
    *,
    resolve_pubkey: Callable[[str], Optional[str]] = _default_resolve_pubkey,
    verify: Callable[[bytes, str, str], bool] = _default_verify,
    max_age: int = 300,
) -> Assertion:
    claim = signed.get("claim") or ""
    sig = signed.get("sig") or ""
    try:
        d = json.loads(claim)
        a = Assertion(
            fqid=d["fqid"], space_id=d["space_id"], issued_at=int(d["issued_at"]), nonce=d["nonce"]
        )
    except Exception as exc:
        raise AssertionError(f"malformed claim: {exc}") from exc
    # M1: a fqid must be a strict `agent@host` — exactly one `@`, both halves
    # non-empty. A malformed fqid (e.g. "@host", "host", "a@b@c") must never
    # reach key resolution / trust policy.
    parts = a.fqid.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise AssertionError(f"malformed fqid {a.fqid!r}")
    pub = resolve_pubkey(a.fqid)
    if not pub:
        raise AssertionError(f"no pubkey for fqid {a.fqid!r}")
    if not verify(claim.encode(), sig, pub):
        raise AssertionError("signature verification failed")
    # I1b: two-sided freshness — reject assertions that are too old AND ones
    # dated too far in the future (clock-skew attack / pre-minted replays). A
    # small future skew (issued_at slightly ahead) within max_age is tolerated.
    if max_age:
        skew = time.time() - a.issued_at
        if skew > max_age:
            raise AssertionError("assertion expired/stale")
        if -skew > max_age:
            raise AssertionError("assertion future-dated")
    return a
