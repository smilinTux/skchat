# SK Spaces — S5 Federation (sk-lk-authd) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. Repo: `skchat`, branch `feat/sk-spaces`. Run tests from repo root: `~/.skenv/bin/python -m pytest tests/ -q`.

**Goal:** Build the federation core — `sk-lk-authd`, the per-host authorization service that verifies a **capauth-signed FQID assertion** from a (possibly remote) host, applies a **trust-graph policy**, and mints a **LiveKit JWT** with the local SFU's secret — plus deterministic focus selection and the signed-Nostr discovery event codec. This is the MSC4195 model: *federated identity + discovery, single SFU per room* (media cascading stays parked).

**Architecture:** All federation code lives in `src/skchat/spaces/federation/`. The crypto (capauth sign/verify), the FQID→pubkey resolution, and the Nostr relay I/O are behind **injectable seams** so the whole flow is unit-testable with no keys/relays/SFU. The `sk-lk-authd` HTTP endpoint (`POST /sfu/get`) mounts on the existing webui. The two-host live test (.158↔.41) is an infra step (Task 7), gated on a second SFU on .41.

**Grounded APIs (recon 2026-06-13):**
- capauth: `from capauth.crypto import get_backend` → `backend.sign(data, priv_armor, passphrase)` / `backend.verify(data, sig_armor, pub_armor)`; `from capauth import resolve_agent_identity` → `AgentIdentity(agent, capauth_uri, fqid, fingerprint)`.
- FQID→pubkey: `from skcomms.mailbox import _load_verifier_key` → `_load_verifier_key(fqid) -> str|None` (armored pubkey).
- LiveKit mint: `from skchat.spaces.tokens import mint_space_token` (signs with `SKCHAT_LIVEKIT_API_KEY/SECRET`).
- Nostr low-level: `skcomms.transports.nostr._make_event/_sign_event/_publish_to_relay/_query_relay` (only NIP-17 DMs are wrapped; custom kinds need a thin codec — built here).

**Spec:** `docs/superpowers/specs/2026-06-13-sk-spaces-design.md` §7. Coord: `d0229242`.

---

## Task 1: Signed FQID assertion (schema + sign/verify, injectable backend)

**Files:** Create `src/skchat/spaces/federation/__init__.py`, `src/skchat/spaces/federation/assertion.py`; Test `tests/test_fed_assertion.py`.

The assertion is what a joining client presents to a (possibly remote) `sk-lk-authd`: "I am FQID X, I want space S, at time T, nonce N" — signed by X's capauth key.

- [ ] **Step 1: Write the failing test** — `tests/test_fed_assertion.py`:

```python
import json
import time

import pytest

from skchat.spaces.federation.assertion import (
    Assertion,
    AssertionError as FedAssertionError,
    build_signed,
    verify_signed,
)


def _fake_sign(payload: bytes) -> str:
    # deterministic stand-in for capauth PGP signing
    return "SIG(" + payload.decode() + ")"


def _fake_verify_ok(payload: bytes, sig: str, pub: str) -> bool:
    return sig == "SIG(" + payload.decode() + ")"


def test_build_and_verify_roundtrip():
    a = Assertion(fqid="lumina@chef.skworld", space_id="space-x",
                  issued_at=int(time.time()), nonce="abc")
    signed = build_signed(a, sign=_fake_sign)
    assert signed["sig"]
    out = verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)
    assert out.fqid == "lumina@chef.skworld"
    assert out.space_id == "space-x"


def test_verify_rejects_bad_signature():
    a = Assertion(fqid="x@y.z", space_id="space-x", issued_at=int(time.time()),
                  nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    signed["sig"] = "SIG(tampered)"
    with pytest.raises(FedAssertionError, match="signature"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_verify_rejects_unknown_signer():
    a = Assertion(fqid="ghost@nowhere", space_id="space-x",
                  issued_at=int(time.time()), nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    with pytest.raises(FedAssertionError, match="pubkey"):
        verify_signed(signed, resolve_pubkey=lambda f: None, verify=_fake_verify_ok)


def test_verify_rejects_stale_assertion():
    a = Assertion(fqid="x@y.z", space_id="space-x",
                  issued_at=int(time.time()) - 9999, nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    with pytest.raises(FedAssertionError, match="expired|stale"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB",
                      verify=_fake_verify_ok, max_age=300)


def test_signed_payload_is_canonical_json():
    a = Assertion(fqid="x@y.z", space_id="s", issued_at=10, nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    # the signed bytes are the canonical (sorted-keys) JSON of the claim
    claim = json.loads(signed["claim"])
    assert claim == {"fqid": "x@y.z", "space_id": "s", "issued_at": 10, "nonce": "n"}
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** `src/skchat/spaces/federation/__init__.py` (docstring + `__all__ = []`) and `assertion.py`:

```python
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
    from skcomms.mailbox import _load_verifier_key
    return _load_verifier_key(fqid)


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
        a = Assertion(fqid=d["fqid"], space_id=d["space_id"],
                      issued_at=int(d["issued_at"]), nonce=d["nonce"])
    except Exception as exc:
        raise AssertionError(f"malformed claim: {exc}") from exc
    pub = resolve_pubkey(a.fqid)
    if not pub:
        raise AssertionError(f"no pubkey for fqid {a.fqid!r}")
    if not verify(claim.encode(), sig, pub):
        raise AssertionError("signature verification failed")
    if max_age and (time.time() - a.issued_at) > max_age:
        raise AssertionError("assertion expired/stale")
    return a
```

- [ ] **Step 4: Run → PASS** (5 tests). **Step 5: Commit** `feat(fed): signed FQID assertion (capauth-backed, injectable)`.

---

## Task 2: Trust policy (per-FQID access level)

**Files:** Create `src/skchat/spaces/federation/trust.py`; Test `tests/test_fed_trust.py`.

No per-FQID trust lookup exists today — build a config-backed one (the
`LIVEKIT_FULL_ACCESS_HOMESERVERS` analog, but per-FQID/host).

- [ ] **Step 1: Failing test** — `tests/test_fed_trust.py`:

```python
from skchat.spaces.federation.trust import AccessLevel, TrustPolicy


def _policy(tmp_path, data):
    import json
    p = tmp_path / "trust.json"
    p.write_text(json.dumps(data))
    return TrustPolicy(path=p)


def test_full_access_host(tmp_path):
    pol = _policy(tmp_path, {"full_access": ["chef.skworld"], "default": "subscribe"})
    assert pol.access_for("lumina@chef.skworld") == AccessLevel.FULL


def test_default_subscribe_for_unknown(tmp_path):
    pol = _policy(tmp_path, {"full_access": ["chef.skworld"], "default": "subscribe"})
    assert pol.access_for("rando@other.realm") == AccessLevel.SUBSCRIBE


def test_default_deny(tmp_path):
    pol = _policy(tmp_path, {"full_access": [], "default": "deny"})
    assert pol.access_for("x@y.z") == AccessLevel.DENY


def test_explicit_fqid_full_access(tmp_path):
    pol = _policy(tmp_path, {"full_access": ["opus@chef.skworld"], "default": "deny"})
    assert pol.access_for("opus@chef.skworld") == AccessLevel.FULL
    assert pol.access_for("other@chef.skworld") == AccessLevel.DENY


def test_missing_config_is_deny_by_default(tmp_path):
    pol = TrustPolicy(path=tmp_path / "nope.json")
    assert pol.access_for("x@y.z") == AccessLevel.DENY
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `trust.py`:

```python
"""Per-FQID trust policy (spec §7) — the allowlist analog, cryptographic by FQID.

Config (~/.skchat/federation-trust.json):
  {"full_access": ["chef.skworld", "opus@chef.skworld"], "default": "subscribe"|"deny"}
An entry matches a full FQID (`a@b.c`) OR a host suffix (`b.c`). `default` applies
to anything unmatched. Missing config => deny (safe default)."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

_DEFAULT_PATH = Path.home() / ".skchat" / "federation-trust.json"


class AccessLevel(str, Enum):
    FULL = "full"          # may publish (speaker/host per role)
    SUBSCRIBE = "subscribe"  # listen-only
    DENY = "deny"          # rejected


class TrustPolicy:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH
        self._full: set[str] = set()
        self._default = AccessLevel.DENY
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._full = set(d.get("full_access", []))
        try:
            self._default = AccessLevel(d.get("default", "deny"))
        except ValueError:
            self._default = AccessLevel.DENY

    def access_for(self, fqid: str) -> AccessLevel:
        host = fqid.split("@", 1)[1] if "@" in fqid else fqid
        if fqid in self._full or host in self._full:
            return AccessLevel.FULL
        return self._default
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(fed): per-FQID trust policy (full/subscribe/deny)`.

---

## Task 3: Deterministic focus selection

**Files:** Create `src/skchat/spaces/federation/focus.py`; Test `tests/test_fed_focus.py`.

Spec §7: the call's SFU = the **oldest valid membership's** preferred focus. Every
federated peer computes the same answer from replicated signed events.

- [ ] **Step 1: Failing test** — `tests/test_fed_focus.py`:

```python
import pytest

from skchat.spaces.federation.focus import Membership, select_focus


def test_oldest_membership_wins():
    ms = [
        Membership(fqid="b@h2", foci_preferred="sfu-2", issued_at=200),
        Membership(fqid="a@h1", foci_preferred="sfu-1", issued_at=100),  # oldest
        Membership(fqid="c@h3", foci_preferred="sfu-3", issued_at=300),
    ]
    assert select_focus(ms) == "sfu-1"


def test_tie_breaks_deterministically_by_fqid():
    ms = [
        Membership(fqid="b@h", foci_preferred="sfu-b", issued_at=100),
        Membership(fqid="a@h", foci_preferred="sfu-a", issued_at=100),
    ]
    assert select_focus(ms) == "sfu-a"  # same ts -> lowest fqid


def test_ignores_memberships_without_a_focus():
    ms = [
        Membership(fqid="a@h", foci_preferred="", issued_at=50),     # no focus
        Membership(fqid="b@h", foci_preferred="sfu-b", issued_at=80),
    ]
    assert select_focus(ms) == "sfu-b"


def test_empty_returns_none():
    assert select_focus([]) is None
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `focus.py`:

```python
"""Deterministic focus (SFU) selection (spec §7): oldest valid membership wins."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Membership:
    fqid: str
    foci_preferred: str
    issued_at: int


def select_focus(memberships: list[Membership]) -> str | None:
    valid = [m for m in memberships if m.foci_preferred]
    if not valid:
        return None
    winner = min(valid, key=lambda m: (m.issued_at, m.fqid))
    return winner.foci_preferred
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(fed): deterministic oldest-membership focus selection`.

---

## Task 4: Federation event codec (Nostr custom kinds)

**Files:** Create `src/skchat/spaces/federation/events.py`; Test `tests/test_fed_events.py`.

Build/parse the three signed-discovery events (focus descriptor, Space state,
membership) as Nostr events (NIP-53 shapes where applicable). Relay I/O is NOT
here — only pure build/parse, so it's testable without relays.

- [ ] **Step 1: Failing test** — `tests/test_fed_events.py`:

```python
from skchat.spaces.federation.events import (
    FOCUS_KIND,
    MEMBERSHIP_KIND,
    SPACE_KIND,
    build_focus_descriptor,
    build_membership,
    build_space_state,
    parse_focus_descriptor,
    parse_membership,
)


def test_focus_descriptor_roundtrip():
    ev = build_focus_descriptor(host_fqid="lumina@chef.skworld",
                                auth_url="https://h/sfu/get",
                                sfu_ws_url="wss://h:8443")
    assert ev["kind"] == FOCUS_KIND
    d = parse_focus_descriptor(ev)
    assert d["host_fqid"] == "lumina@chef.skworld"
    assert d["auth_url"] == "https://h/sfu/get"
    assert d["sfu_ws_url"] == "wss://h:8443"


def test_membership_roundtrip_carries_foci_preferred():
    ev = build_membership(fqid="opus@chef.skworld", space_id="space-x",
                          foci_preferred="lumina@chef.skworld", issued_at=123)
    assert ev["kind"] == MEMBERSHIP_KIND
    m = parse_membership(ev)
    assert m.fqid == "opus@chef.skworld"
    assert m.foci_preferred == "lumina@chef.skworld"
    assert m.issued_at == 123


def test_space_state_has_kind_and_title():
    ev = build_space_state(space_id="space-x", title="Town Hall",
                           host_fqid="lumina@chef.skworld", status="live")
    assert ev["kind"] == SPACE_KIND
    assert any(t == ["title", "Town Hall"] for t in ev["tags"])
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `events.py` (build dict events with `kind`/`tags`/`content`; `parse_*` read them back; reuse `focus.Membership`):

```python
"""Signed federation discovery events (spec §7) as Nostr events.

NIP-53-aligned kinds: 30312 = Space state, 10312 = membership/presence; a custom
30078-style app-data kind for the focus descriptor. Only build/parse here; the
relay publish/query I/O lives in nostr_io.py (Task 5) behind an injectable seam.
"""

from __future__ import annotations

import json

from skchat.spaces.federation.focus import Membership

FOCUS_KIND = 30078       # app-specific: SFU focus descriptor
SPACE_KIND = 30312       # NIP-53 live room
MEMBERSHIP_KIND = 10312  # NIP-53 room presence/membership


def build_focus_descriptor(*, host_fqid: str, auth_url: str, sfu_ws_url: str) -> dict:
    return {
        "kind": FOCUS_KIND,
        "tags": [["d", "sk-lk-focus"], ["host", host_fqid]],
        "content": json.dumps({"type": "livekit", "host_fqid": host_fqid,
                               "auth_url": auth_url, "sfu_ws_url": sfu_ws_url}),
    }


def parse_focus_descriptor(ev: dict) -> dict:
    return json.loads(ev.get("content") or "{}")


def build_space_state(*, space_id: str, title: str, host_fqid: str,
                      status: str) -> dict:
    return {
        "kind": SPACE_KIND,
        "tags": [["d", space_id], ["title", title], ["host", host_fqid],
                 ["status", status]],
        "content": "",
    }


def build_membership(*, fqid: str, space_id: str, foci_preferred: str,
                     issued_at: int) -> dict:
    return {
        "kind": MEMBERSHIP_KIND,
        "tags": [["a", f"{SPACE_KIND}:{space_id}"], ["fqid", fqid],
                 ["foci_preferred", foci_preferred]],
        "content": "",
        "created_at": issued_at,
    }


def parse_membership(ev: dict) -> Membership:
    tags = {t[0]: t[1] for t in ev.get("tags", []) if len(t) >= 2}
    return Membership(fqid=tags.get("fqid", ""),
                      foci_preferred=tags.get("foci_preferred", ""),
                      issued_at=int(ev.get("created_at", 0)))
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(fed): Nostr discovery event codec (focus/space/membership)`.

---

## Task 5: sk-lk-authd orchestration + `/sfu/get` route

**Files:** Create `src/skchat/spaces/federation/authd.py`; Modify `src/skchat/spaces/routes.py` (mount the route); Test `tests/test_fed_authd.py`.

- [ ] **Step 1: Failing test** — `tests/test_fed_authd.py`:

```python
import time

import pytest

from skchat.spaces.federation.assertion import Assertion, build_signed
from skchat.spaces.federation.authd import AuthDenied, authorize
from skchat.spaces.federation.trust import AccessLevel


def _signed(fqid, space):
    a = Assertion(fqid=fqid, space_id=space, issued_at=int(time.time()), nonce="n")
    return build_signed(a, sign=lambda p: "SIG")


def _verify_to(fqid):
    # inject a verifier that always returns this fqid as verified
    def _v(signed, **kw):
        return Assertion(fqid=fqid, space_id=signed_space(signed),
                         issued_at=int(time.time()), nonce="n")
    return _v


def signed_space(signed):
    import json
    return json.loads(signed["claim"])["space_id"]


def test_full_access_gets_host_token():
    out = authorize(
        _signed("lumina@chef.skworld", "space-x"),
        sfu_ws_url="wss://h:8443",
        _verify=_verify_to("lumina@chef.skworld"),
        _access=lambda f: AccessLevel.FULL,
        _mint=lambda identity, role, space: f"TOKEN:{role}:{space}",
    )
    assert out["sfu_ws_url"] == "wss://h:8443"
    assert out["role"] in ("host", "speaker")
    assert out["token"].startswith("TOKEN:")


def test_subscribe_access_gets_listener_token():
    out = authorize(
        _signed("rando@other", "space-x"),
        sfu_ws_url="wss://h:8443",
        _verify=_verify_to("rando@other"),
        _access=lambda f: AccessLevel.SUBSCRIBE,
        _mint=lambda identity, role, space: f"TOKEN:{role}",
    )
    assert out["role"] == "listener"


def test_denied_access_raises():
    with pytest.raises(AuthDenied):
        authorize(
            _signed("ghost@nowhere", "space-x"),
            sfu_ws_url="wss://h:8443",
            _verify=_verify_to("ghost@nowhere"),
            _access=lambda f: AccessLevel.DENY,
            _mint=lambda *a: "X",
        )
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `authd.py` (verify → access → mint; FULL→speaker, SUBSCRIBE→listener, DENY→raise). Default seams use Task 1 `verify_signed`, Task 2 `TrustPolicy.access_for`, and `mint_space_token`:

```python
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
```

- [ ] **Step 4:** Mount the route in `routes.py` (`POST /sfu/get` → read `{claim, sig}` body → `authorize(signed, sfu_ws_url=_url())` → 403 on `AuthDenied`/assertion errors). Add a route test in `tests/test_fed_authd.py` using TestClient with injected seams via env or a module hook (keep it simple: assert a malformed body → 400, a valid path is covered by the unit tests above).

- [ ] **Step 5: Run → PASS. Step 6: Commit** `feat(fed): sk-lk-authd authorize() + /sfu/get route`.

---

## Task 6: Nostr relay I/O wrapper (publish/query federation events)

**Files:** Create `src/skchat/spaces/federation/nostr_io.py`; Test `tests/test_fed_nostr_io.py`.

Wrap the skcomms nostr low-level (`_make_event/_sign_event/_publish_to_relay/_query_relay`) so a host can publish its focus descriptor + Space state, and a client can query memberships. Relay calls behind an injectable `publish`/`query` seam → testable with fakes (no network).

- [ ] Build `FederationNostr` with `publish_focus(...)`, `publish_space(...)`, `publish_membership(...)`, `query_memberships(space_id) -> list[Membership]` (parse via Task 4). Tests inject fake publish/query callables and assert the right kinds/filters are used and memberships parse. Commit `feat(fed): nostr relay I/O for federation events`.

---

## Task 7 (INFRA — needs the .41 SFU): two-host live test

Not a pytest task — a runbook + the actual two-host wiring. Gated on a second LiveKit SFU on .41.

- [ ] Create `runbooks/spaces-federation-2host.md`: (1) stand up a `livekit-server` on **.41** (mirror the .158 systemd unit + a `livekit.yaml` with .41's tailnet bind + its own keys); (2) run a `sk-lk-authd` `/sfu/get` on both hosts (it's mounted on each webui); (3) write `~/.skchat/federation-trust.json` on .158 granting `chef.skworld` full access; (4) the live test: an FQID resident on **.41** builds a signed assertion for a Space **hosted on .158**, POSTs `.158/sfu/get`, gets a token for the .158 SFU, and connects over the tailnet — proving cross-host federation with media on the single (host's) SFU.
- [ ] Add `deploy/v2/` notes for the .41 SFU unit. Document that media stays on the **host's** SFU (single-SFU-per-room; cascading parked).

---

## Final verification

- [ ] `~/.skenv/bin/python -m pytest tests/test_fed_*.py -v && ~/.skenv/bin/python -m pytest tests/ -q` → all federation + full suite pass, no regressions.
- [ ] `~/.skenv/bin/ruff check src/skchat/spaces/federation/`.

## What S5 delivers

The federation core: a sovereign host can verify a **capauth-signed FQID assertion** from any host, apply a **trust policy**, and mint a LiveKit token for **its own SFU** — with deterministic **oldest-membership** focus selection and a **signed-Nostr** discovery codec. Tasks 1–6 are CI-tested locally on .158; Task 7 lights up the real .158↔.41 cross-host join once the second SFU is deployed. Media cascading across SFUs stays parked (unsolved upstream) — the seam is left for it.
```
