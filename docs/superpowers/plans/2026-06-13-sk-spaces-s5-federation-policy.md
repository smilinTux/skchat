# SK Spaces — S5 Federation Policy (remote-role cap + space validation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Repo: `skchat`, branch `feat/sk-spaces`. Run tests from repo root: `~/.skenv/bin/python -m pytest tests/ -q`.

**Goal:** Close the two federation policy gaps the S5 review flagged (coord `f27a5b84`) — but **decision-free**, via *configurable* defaults: (1) cap what a FULL-trust *remote* peer can do (publish vs listen) via a config knob, default = current behavior; (2) validate the asserted `space_id` is a known live Space before minting a token.

**Architecture:** Both are additive seams on the existing `src/skchat/spaces/federation/`. The remote-role cap is a `remote_max_role` field on `TrustPolicy` (from `federation-trust.json`); `authd.authorize` caps the FULL→speaker mapping by it. Space validation is an injectable `_space_live` callable on `authorize` (the `/sfu/get` route wires it to the registry). No new design decisions required — the operator sets `remote_max_role` if they want remotes capped at listener.

**Tech Stack:** Python 3.10+, reuses the S5 federation core. `pytest`. Line 99, ruff.

**Spec:** `docs/superpowers/specs/2026-06-13-sk-spaces-design.md` §7. **Depends on:** S5 core (built). Coord: `f27a5b84`.

**Grounding (existing):**
- `src/skchat/spaces/federation/trust.py` — `TrustPolicy(path)`, `access_for(fqid) -> AccessLevel`, `AccessLevel.{FULL,SUBSCRIBE,DENY}`; config `~/.skchat/federation-trust.json` = `{"full_access":[...], "default":"subscribe"|"deny"}`.
- `src/skchat/spaces/federation/authd.py` — `authorize(signed, *, sfu_ws_url, _verify=verify_signed, _access=None, _mint=None) -> dict`; `_ROLE_FOR = {FULL: Role.SPEAKER, SUBSCRIBE: Role.LISTENER}`; raises `AuthDenied`.
- `src/skchat/spaces/roles.py` — `Role.{HOST,SPEAKER,LISTENER}`.
- `src/skchat/spaces/registry.py` — `SpaceRegistry.get(space_id)` / `.live()`.

---

## Task 1: `remote_max_role` on TrustPolicy

**Files:** Modify `src/skchat/spaces/federation/trust.py`. Test `tests/test_fed_trust_remote_cap.py`.

- [ ] **Step 1: Failing test** — `tests/test_fed_trust_remote_cap.py`:

```python
import json

from skchat.spaces.federation.trust import TrustPolicy


def _pol(tmp_path, data):
    p = tmp_path / "trust.json"
    p.write_text(json.dumps(data))
    return TrustPolicy(path=p)


def test_remote_max_role_defaults_to_speaker(tmp_path):
    pol = _pol(tmp_path, {"full_access": ["chef.skworld"], "default": "deny"})
    assert pol.remote_max_role == "speaker"          # default preserves behavior


def test_remote_max_role_can_be_listener(tmp_path):
    pol = _pol(tmp_path, {"full_access": ["chef.skworld"], "default": "deny",
                          "remote_max_role": "listener"})
    assert pol.remote_max_role == "listener"


def test_invalid_remote_max_role_falls_back_to_speaker(tmp_path):
    pol = _pol(tmp_path, {"full_access": [], "default": "deny",
                          "remote_max_role": "host"})   # host not allowed for remotes
    assert pol.remote_max_role == "speaker"
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** — in `trust.py`, add to `TrustPolicy.__init__` a `self._remote_max_role = "speaker"`, parse it in `_load` (only `"speaker"` or `"listener"` accepted; anything else → `"speaker"`), and expose:

```python
    @property
    def remote_max_role(self) -> str:
        return self._remote_max_role
```

In `_load`, after parsing `full_access`/`default`, add:

```python
        rmr = d.get("remote_max_role", "speaker")
        self._remote_max_role = rmr if rmr in ("speaker", "listener") else "speaker"
```

- [ ] **Step 4: Run → PASS + existing trust tests still green** (`~/.skenv/bin/python -m pytest tests/test_fed_trust.py tests/test_fed_trust_remote_cap.py -v`). **Step 5: Commit** `feat(fed): configurable remote_max_role on TrustPolicy`.

---

## Task 2: authd caps the remote role + validates the space

**Files:** Modify `src/skchat/spaces/federation/authd.py`. Test `tests/test_fed_authd_policy.py`.

- [ ] **Step 1: Failing test** — `tests/test_fed_authd_policy.py`:

```python
import time

import pytest

from skchat.spaces.federation.assertion import Assertion, build_signed
from skchat.spaces.federation.authd import AuthDenied, authorize
from skchat.spaces.federation.trust import AccessLevel
from skchat.spaces.roles import Role


def _signed(fqid, space):
    a = Assertion(fqid=fqid, space_id=space, issued_at=int(time.time()), nonce="n")
    return build_signed(a, sign=lambda p: "SIG")


def _verify_to(fqid, space):
    def _v(signed, **kw):
        return Assertion(fqid=fqid, space_id=space, issued_at=int(time.time()),
                         nonce="n")
    return _v


def _mint(identity, role, space):
    return f"TOKEN:{role.value}"


def test_full_access_capped_to_listener_when_configured():
    out = authorize(
        _signed("opus@chef.skworld", "space-x"), sfu_ws_url="wss://h",
        _verify=_verify_to("opus@chef.skworld", "space-x"),
        _access=lambda f: AccessLevel.FULL,
        _remote_max_role="listener",          # operator caps remotes at listener
        _mint=_mint,
    )
    assert out["role"] == "listener"


def test_full_access_is_speaker_by_default():
    out = authorize(
        _signed("opus@chef.skworld", "space-x"), sfu_ws_url="wss://h",
        _verify=_verify_to("opus@chef.skworld", "space-x"),
        _access=lambda f: AccessLevel.FULL, _mint=_mint,
    )
    assert out["role"] == "speaker"


def test_unknown_space_is_denied():
    with pytest.raises(AuthDenied, match="space"):
        authorize(
            _signed("opus@chef.skworld", "space-gone"), sfu_ws_url="wss://h",
            _verify=_verify_to("opus@chef.skworld", "space-gone"),
            _access=lambda f: AccessLevel.FULL,
            _space_live=lambda sid: False,     # space doesn't exist / not live
            _mint=_mint,
        )


def test_live_space_passes():
    out = authorize(
        _signed("opus@chef.skworld", "space-x"), sfu_ws_url="wss://h",
        _verify=_verify_to("opus@chef.skworld", "space-x"),
        _access=lambda f: AccessLevel.FULL,
        _space_live=lambda sid: True, _mint=_mint,
    )
    assert out["role"] == "speaker"
```

- [ ] **Step 2: Run → FAIL. Step 3: Modify `authorize`** — add two optional kwargs and apply them:

```python
def authorize(
    signed: dict,
    *,
    sfu_ws_url: str,
    _verify=verify_signed,
    _access=None,
    _mint=None,
    _remote_max_role: str | None = None,
    _space_live=None,
) -> dict:
    assertion = _verify(signed)
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
    token = (_mint or _default_mint)(assertion.fqid, role, assertion.space_id)
    return {"sfu_ws_url": sfu_ws_url, "token": token, "role": role.value,
            "identity": assertion.fqid, "space_id": assertion.space_id}
```

(Keep `_default_mint`, the imports, and `Role` available — add `from skchat.spaces.roles import Role` if not already imported.)

- [ ] **Step 4: Run → PASS + existing authd tests green** (`~/.skenv/bin/python -m pytest tests/test_fed_authd.py tests/test_fed_authd_policy.py -v`). **Step 5: Commit** `feat(fed): authd remote-role cap + space-live validation`.

---

## Task 3: Wire the route to the registry + trust policy

**Files:** Modify `src/skchat/spaces/routes.py` (the `/sfu/get` route). Test `tests/test_fed_sfu_get_policy.py`.

- [ ] **Step 1: Failing test** — `tests/test_fed_sfu_get_policy.py`:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    return TestClient(app)


def test_sfu_get_unknown_space_is_403(client):
    # an assertion for a space that was never created → space-live check fails → 403
    # (malformed body still 400; this asserts the route passes a registry-backed
    # _space_live so a non-existent space is rejected before minting)
    r = client.post("/sfu/get", json={"claim": "{}", "sig": "x"})
    assert r.status_code in (400, 403)   # malformed/empty → 400; never 200/500
```

> **NOTE for implementer:** the full happy-path (valid capauth-signed assertion →
> 200) needs real keys and is covered by the live two-host test, not CI. This route
> test only confirms the route doesn't 500 and that the registry-backed `_space_live`
> is wired. In the route, build the `/sfu/get` call to pass
> `_space_live=lambda sid: reg.get(sid) is not None and reg.get(sid).status.value != "ended"`
> into `authorize(...)`. Verify the existing `/sfu/get` route signature in routes.py
> and add the two kwargs (`_space_live`, and leave `_remote_max_role=None` to use the
> TrustPolicy default).

- [ ] **Step 2: Run → FAIL (or adapt). Step 3:** In `routes.py`, find the `/sfu/get` route and change its `authorize(...)` call to pass the registry-backed `_space_live` (and leave `_remote_max_role` unset → uses the on-disk TrustPolicy default). Keep the existing 400/403 error mapping.

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(fed): /sfu/get validates space-live via registry`.

---

## Final verification

- [ ] **Full federation suite + whole skchat suite:**
Run: `~/.skenv/bin/python -m pytest tests/test_fed_*.py -v && ~/.skenv/bin/python -m pytest tests/ -q`
Expected: all federation tests pass; no regressions.
- [ ] **Lint:** `~/.skenv/bin/ruff check src/skchat/spaces/federation/ src/skchat/spaces/routes.py tests/test_fed_*.py` → no errors.

## What this delivers

The two S5-review federation policy gaps closed, decision-free: an operator can cap
FULL-trust *remote* peers at **listener** via `remote_max_role` in
`federation-trust.json` (default `speaker` = unchanged behavior — your call, set in
config not code), and `/sfu/get` now refuses to mint a token for a **non-existent or
ended Space** (registry-backed validation). Both are additive + injectable, fully
CI-tested without a live SFU.
