# Device Management ("Linked Devices") Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator a "Linked Devices" screen that lists every device enrolled under their identity and unlinks any of them, where unlinking immediately kills the device's sessions AND stops message fanout from reaching it.

**Architecture:** A new per-node JSON registry keyed by `device_fp` becomes the join table between the three identifiers a device currently carries (`device_fp` in auth, `key_id` in prekey slots, throwaway `device_id` inside a slot). Enrollment writes the row; prekey publish attaches the `key_id` by reading the authenticated session; unlink walks the row to revoke across all four stores. A `revoked_device_fps` set in the existing SQLite revocation DB kills sessions by fingerprint rather than by jti, so one write revokes every session a device holds.

**Tech Stack:** Python 3.10+, FastAPI, SQLite (stdlib `sqlite3`), pytest. Flutter/Dart + Dio for the app.

**Spec:** `docs/superpowers/specs/2026-08-02-device-management-design.md` (read R1-R4 first; they supersede the original decisions where they conflict).

## Global Constraints

- **No em dashes or en dashes anywhere** (code, comments, docstrings, commit messages, UI copy). Regular hyphens are fine. This is a hard house rule.
- Line length 99 chars; `ruff` (E, W, F, I; E501 ignored) must stay clean on files you touch.
- Run pytest from `~` to avoid the `skmemory` namespace collision: `cd ~ && ~/.skenv/bin/python -m pytest <path> -q`.
- Server work happens in the worktree `/home/cbrd21/skworld-worktrees/s3-devmgmt` on branch `feat/device-management`.
- **App work (Tasks 8-9) must be built and tested on .41**, using the non-snap `~/flutter`. `flutter analyze` locally requires `--no-fatal-infos` (the repo carries ~50 tolerated INFO lints); only `warning`/`error` should fail you.
- Atomic file writes use the existing tmp + `os.replace` pattern (see `operator_auth.DeviceStore.enroll`).
- Never log a full device pubkey or a session token. `device_fp` is fine to log.
- Commit after every task.

## Environment reality (verify before you start)

The live daemon runs `SKCHAT_DATAPLANE_AUTH=1` and `SKCHAT_AUTHZ_PDP=enforce`. Two consequences that shape several tasks:

1. `enforce_dataplane_auth` **returns early when `SKCHAT_DATAPLANE_AUTH` is unset** (the default in tests and dev). When the gate is off there is no verified session, so `request.state.operator_session` will be absent and registry recording is a no-op. Every task below treats "no session" as a normal, non-fatal path.
2. Under `SKCHAT_AUTHZ_PDP=enforce`, a gated route with **no capability mapping fails closed (403)**, and `tests/test_dataplane_coverage.py` enumerates the live route table and fails CI for any gated route that is neither capability-mapped nor self-auth. Task 7 exists solely to keep that gate green. Do not skip it.

## File Structure

| File | Responsibility |
|---|---|
| `src/skchat/device_registry.py` (create) | The registry store: load/save, record enroll, attach key_id, touch, list, mark revoked. Pure store, no HTTP, no revocation logic. |
| `src/skchat/device_unlink.py` (create) | The `unlink()` orchestration across the four stores. Imports the registry, prekeys, DeviceStore, capauth. No HTTP. |
| `src/skchat/guest.py` (modify) | Add the `revoked_device_fps` table + `revoke_device` / `is_device_revoked`, mirroring the existing `revoked_jtis` cache pattern. |
| `src/skchat/operator_auth.py` (modify) | `verify_operator_session` also rejects a revoked `device_fp`. `DeviceStore` gains `list_fps()` and `remove()`. |
| `src/skchat/dataplane_auth.py` (modify) | Stash the verified session on `request.state.operator_session` (R3); add the 3 new routes to `_ROUTE_CAPABILITY_RULES`. |
| `src/skchat/operator_auth_routes.py` (modify) | Accept the optional signed `label` (R2); call `record_enroll`. |
| `src/skchat/daemon_proxy.py` (modify) | `api_publish_prekey` calls `record_publish`. |
| `src/skchat/device_routes.py` (create) | The three operator endpoints. Thin: parse, authorize, delegate to registry/unlink. |
| `src/skchat/cli.py` (modify) | `skchat devices reset --yes` (R1). |
| `tests/test_device_registry.py` (create) | Registry store unit tests. |
| `tests/test_device_unlink.py` (create) | Unlink semantics, including the end-to-end fanout assertion. |
| `tests/test_device_routes.py` (create) | The three endpoints through a real FastAPI TestClient. |
| `tests/test_operator_enroll_label.py` (create) | R2 signature-binding tests. |

App repo (`~/clawd/skcapstone-repos/skworld-app`, separate branch + PR):

| File | Responsibility |
|---|---|
| `lib/services/device_list_service.dart` (create) | Dio calls to the three endpoints. |
| `lib/features/profile/linked_devices_screen.dart` (create) | The Linked Devices list UI. |
| `lib/services/operator_session_service.dart` (modify) | Send the signed `label` at enroll. |

---

### Task 1: Device registry store

**Files:**
- Create: `src/skchat/device_registry.py`
- Test: `tests/test_device_registry.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `registry_path() -> Path`
  - `record_enroll(device_fp: str, *, label: str, label_source: str, platform: str, user_agent: str) -> None`
  - `record_publish(device_fp: str, key_id: str) -> None`
  - `touch(device_fp: str) -> None`
  - `list_devices(*, include_revoked: bool = False) -> list[dict]`
  - `get_device(device_fp: str) -> dict | None`
  - `mark_revoked(device_fp: str) -> bool`
  - `clear_all() -> int`

The store path is `~/.skchat/state/operator_device_registry.json`, overridable via `SKCHAT_DEVICE_REGISTRY` so tests never touch the real home.

- [ ] **Step 1: Write the failing tests**

```python
"""Device registry store: the join table between device_fp, key_id and metadata."""

from __future__ import annotations

import json

import pytest

from skchat import device_registry as DR


@pytest.fixture(autouse=True)
def _registry(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    yield tmp_path


def test_record_enroll_creates_a_row_with_metadata():
    DR.record_enroll(
        "a1b2c3d4e5f60718",
        label="Pixel 8",
        label_source="client",
        platform="android",
        user_agent="Dart/3.5 (dart:io)",
    )
    rows = DR.list_devices()
    assert len(rows) == 1
    row = rows[0]
    assert row["device_fp"] == "a1b2c3d4e5f60718"
    assert row["label"] == "Pixel 8"
    assert row["label_source"] == "client"
    assert row["platform"] == "android"
    assert row["key_ids"] == []
    assert row["revoked"] is False
    assert row["enrolled_at"] > 0
    assert row["last_seen"] >= row["enrolled_at"]


def test_record_publish_attaches_a_key_id_without_duplicating():
    DR.record_enroll("aa" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    DR.record_publish("aa" * 8, "f8342853f762fd88")
    DR.record_publish("aa" * 8, "f8342853f762fd88")  # republish, same slot
    DR.record_publish("aa" * 8, "1111111111111111")  # a second slot
    row = DR.get_device("aa" * 8)
    assert row["key_ids"] == ["f8342853f762fd88", "1111111111111111"]


def test_record_publish_for_an_unknown_device_is_a_no_op_not_a_crash():
    # A publish can arrive from a device enrolled before the registry existed,
    # or with the auth gate off. It must never 500 the publish route.
    DR.record_publish("ff" * 8, "abc")
    assert DR.get_device("ff" * 8) is None


def test_mark_revoked_hides_the_row_by_default_but_keeps_it_for_audit():
    DR.record_enroll("bb" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    assert DR.mark_revoked("bb" * 8) is True
    assert DR.list_devices() == []
    kept = DR.list_devices(include_revoked=True)
    assert len(kept) == 1 and kept[0]["revoked"] is True
    assert DR.mark_revoked("nosuchdevice") is False


def test_a_corrupt_registry_degrades_to_empty_never_raises():
    DR.registry_path().parent.mkdir(parents=True, exist_ok=True)
    DR.registry_path().write_text("{not json at all")
    assert DR.list_devices() == []


def test_clear_all_empties_the_store_and_reports_the_count():
    DR.record_enroll("cc" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    DR.record_enroll("dd" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    assert DR.clear_all() == 2
    assert DR.list_devices(include_revoked=True) == []


def test_touch_bumps_last_seen_only():
    DR.record_enroll("ee" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    before = DR.get_device("ee" * 8)
    DR.touch("ee" * 8)
    after = DR.get_device("ee" * 8)
    assert after["last_seen"] >= before["last_seen"]
    assert after["enrolled_at"] == before["enrolled_at"]


def test_the_stored_file_is_valid_json_keyed_by_device_fp():
    DR.record_enroll("0f" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    data = json.loads(DR.registry_path().read_text())
    assert list(data.keys()) == ["0f" * 8]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_registry.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'skchat.device_registry'`

- [ ] **Step 3: Write the implementation**

```python
"""Operator device registry: the join table for "Linked Devices".

A device carries three unrelated identifiers today (``device_fp`` in the auth
handshake, ``key_id`` on a prekey slot, and a throwaway ``device_id`` inside the
slot) across four stores, with nothing tying them together. This module is that
missing correlation key: one row per ``device_fp``, carrying the metadata the UI
shows and the ``key_ids`` that unlink must remove.

Per-node by design, like the rest of skchat state: a device is managed on the
node it talks to (see the spec's "Out of scope").

Recording is BEST-EFFORT everywhere. A publish from a device that predates the
registry (or that arrived with the auth gate off, so no session was verified)
records nothing and must never break the caller's request.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("skchat.device_registry")

#: Override the store location (tests point this at tmp_path).
REGISTRY_PATH_ENV = "SKCHAT_DEVICE_REGISTRY"
_DEFAULT_REGISTRY = "~/.skchat/state/operator_device_registry.json"

_lock = threading.Lock()


def registry_path() -> Path:
    raw = os.getenv(REGISTRY_PATH_ENV, "").strip() or _DEFAULT_REGISTRY
    return Path(raw).expanduser()


def _load() -> dict[str, dict]:
    """Read the registry. A missing or corrupt file degrades to empty, never raises."""
    path = registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except (ValueError, OSError):
        logger.warning("device registry unreadable, treating as empty: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict]) -> None:
    """Atomic write: tmp in the same dir, then os.replace (never a torn file)."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def record_enroll(
    device_fp: str,
    *,
    label: str,
    label_source: str,
    platform: str,
    user_agent: str,
) -> None:
    """Create (or refresh) the row for a freshly enrolled device.

    A re-enroll of the same fingerprint refreshes the metadata and clears the
    revoked flag: re-linking a device is how you undo an unlink.
    """
    if not device_fp:
        return
    now = time.time()
    with _lock:
        data = _load()
        existing = data.get(device_fp) or {}
        data[device_fp] = {
            "device_fp": device_fp,
            "label": label,
            "label_source": label_source,
            "platform": platform,
            "user_agent": user_agent,
            "enrolled_at": now,
            "last_seen": now,
            "key_ids": list(existing.get("key_ids") or []),
            "revoked": False,
        }
        _save(data)


def record_publish(device_fp: str, key_id: str) -> None:
    """Attach a published prekey slot to its device. No-op for unknown devices."""
    if not (device_fp and key_id):
        return
    with _lock:
        data = _load()
        row = data.get(device_fp)
        if row is None:
            return
        key_ids = list(row.get("key_ids") or [])
        if key_id not in key_ids:
            key_ids.append(key_id)
        row["key_ids"] = key_ids
        row["last_seen"] = time.time()
        data[device_fp] = row
        _save(data)


def touch(device_fp: str) -> None:
    """Bump ``last_seen``. Best-effort; an unknown device is ignored."""
    if not device_fp:
        return
    with _lock:
        data = _load()
        row = data.get(device_fp)
        if row is None:
            return
        row["last_seen"] = time.time()
        data[device_fp] = row
        _save(data)


def get_device(device_fp: str) -> dict | None:
    with _lock:
        return _load().get(device_fp)


def list_devices(*, include_revoked: bool = False) -> list[dict]:
    """Rows, newest enrollment first. Revoked rows are kept for audit but hidden."""
    with _lock:
        rows = list(_load().values())
    if not include_revoked:
        rows = [r for r in rows if not r.get("revoked")]
    return sorted(rows, key=lambda r: r.get("enrolled_at") or 0, reverse=True)


def mark_revoked(device_fp: str) -> bool:
    """Flag a row revoked. True if a row was found and flagged."""
    with _lock:
        data = _load()
        row = data.get(device_fp)
        if row is None:
            return False
        row["revoked"] = True
        data[device_fp] = row
        _save(data)
        return True


def clear_all() -> int:
    """Wipe every row (the R1 clean cut). Returns how many were removed."""
    with _lock:
        data = _load()
        count = len(data)
        _save({})
        return count
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_registry.py -q`
Expected: PASS, 8 passed

- [ ] **Step 5: Lint**

Run: `cd /home/cbrd21/skworld-worktrees/s3-devmgmt && ~/.skenv/bin/ruff check src/skchat/device_registry.py tests/test_device_registry.py && ~/.skenv/bin/ruff format --check --line-length 99 src/skchat/device_registry.py tests/test_device_registry.py`
Expected: "All checks passed!" and "2 files already formatted"

- [ ] **Step 6: Commit**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git add src/skchat/device_registry.py tests/test_device_registry.py
git commit -m "feat(devices): device registry store, the device_fp to key_id join table"
```

---

### Task 2: Revoked-device_fp set and the session kill

**Files:**
- Modify: `src/skchat/guest.py` (add alongside the existing `revoked_jtis` machinery around lines 100-205)
- Modify: `src/skchat/operator_auth.py:64-87` (`verify_operator_session`)
- Test: `tests/test_device_revocation.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `skchat.guest.revoke_device(device_fp: str) -> None`
  - `skchat.guest.unrevoke_device(device_fp: str) -> None`
  - `skchat.guest.is_device_revoked(device_fp: str) -> bool`
  - `skchat.guest._reset_device_revocation_cache() -> None` (test seam, mirrors `_reset_revocation_cache`)
  - `verify_operator_session` now raises `OperatorAuthError("device revoked")` for a revoked fp.

- [ ] **Step 1: Write the failing tests**

```python
"""Revocation by device_fp: one write kills every session a device holds."""

from __future__ import annotations

import pytest

from skchat import guest as G
from skchat import operator_auth as OA


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    yield


def test_a_revoked_device_fp_is_reported_revoked_and_survives_a_cache_drop():
    assert G.is_device_revoked("aa" * 8) is False
    G.revoke_device("aa" * 8)
    assert G.is_device_revoked("aa" * 8) is True
    # Simulate a process restart: the SQLite row, not the cache, is the truth.
    G._reset_device_revocation_cache()
    assert G.is_device_revoked("aa" * 8) is True


def test_every_session_of_a_revoked_device_dies_at_once():
    fp = "bb" * 8
    first = OA.mint_operator_session(device_fp=fp)
    second = OA.mint_operator_session(device_fp=fp)
    assert OA.verify_operator_session(first).device_fp == fp
    assert OA.verify_operator_session(second).device_fp == fp

    G.revoke_device(fp)

    # BOTH sessions die from the single revocation, without either jti being known.
    for token in (first, second):
        with pytest.raises(OA.OperatorAuthError):
            OA.verify_operator_session(token)


def test_revoking_one_device_leaves_another_devices_session_working():
    keep = OA.mint_operator_session(device_fp="cc" * 8)
    G.revoke_device("dd" * 8)
    assert OA.verify_operator_session(keep).device_fp == "cc" * 8


def test_unrevoke_lets_a_relinked_device_authenticate_again():
    fp = "ee" * 8
    G.revoke_device(fp)
    token_after_relink = OA.mint_operator_session(device_fp=fp)
    with pytest.raises(OA.OperatorAuthError):
        OA.verify_operator_session(token_after_relink)
    G.unrevoke_device(fp)
    assert OA.verify_operator_session(token_after_relink).device_fp == fp


def test_revoke_device_is_idempotent():
    G.revoke_device("ff" * 8)
    G.revoke_device("ff" * 8)
    assert G.is_device_revoked("ff" * 8) is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_revocation.py -q`
Expected: FAIL, `AttributeError: module 'skchat.guest' has no attribute '_reset_device_revocation_cache'`

- [ ] **Step 3: Add the store to `guest.py`**

Add the table to `_connect()` (immediately after the existing `used_jtis` CREATE TABLE, before `conn.commit()`):

```python
    conn.execute(
        "CREATE TABLE IF NOT EXISTS revoked_device_fps ("
        "  device_fp TEXT PRIMARY KEY,"
        "  revoked_at REAL NOT NULL"
        ")"
    )
```

Then add this block directly after the existing `_is_revoked` function:

```python
# ── Revocation by device fingerprint (Linked Devices) ───────────────────────
# Session kill for a whole DEVICE rather than a single jti. Operator sessions
# are stateless JWTs with no server-side registry, so there is no way to
# enumerate the jtis a device currently holds. Keying revocation on the
# ``device_fp`` claim instead means one write kills every session that device
# has ever been issued, including any minted a second before the unlink.
# Same cache-fronts-SQLite shape as the jti set above.

_revoked_devices: set[str] = set()
_revoked_devices_loaded = False


def _reset_device_revocation_cache() -> None:
    """Drop the in-memory device cache so the next check re-reads the DB."""
    global _revoked_devices_loaded
    with _store_lock:
        _revoked_devices.clear()
        _revoked_devices_loaded = False


def _load_revoked_devices(conn: sqlite3.Connection) -> None:
    global _revoked_devices_loaded
    rows = conn.execute("SELECT device_fp FROM revoked_device_fps").fetchall()
    _revoked_devices.clear()
    _revoked_devices.update(r[0] for r in rows)
    _revoked_devices_loaded = True


def revoke_device(device_fp: str) -> None:
    """Revoke every session belonging to *device_fp*. Idempotent."""
    if not device_fp:
        return
    with _store_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO revoked_device_fps (device_fp, revoked_at) VALUES (?, ?)",
                (device_fp, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        _revoked_devices.add(device_fp)


def unrevoke_device(device_fp: str) -> None:
    """Clear a device revocation, so re-enrolling the same key works again."""
    if not device_fp:
        return
    with _store_lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM revoked_device_fps WHERE device_fp = ?", (device_fp,))
            conn.commit()
        finally:
            conn.close()
        _revoked_devices.discard(device_fp)


def is_device_revoked(device_fp: str) -> bool:
    """True if *device_fp* is revoked. Cache-first; the DB is the source of truth."""
    if not device_fp:
        return False
    with _store_lock:
        if device_fp in _revoked_devices:
            return True
        conn = _connect()
        try:
            if not _revoked_devices_loaded:
                _load_revoked_devices(conn)
                if device_fp in _revoked_devices:
                    return True
            row = conn.execute(
                "SELECT 1 FROM revoked_device_fps WHERE device_fp = ?", (device_fp,)
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            _revoked_devices.add(device_fp)
            return True
        return False
```

- [ ] **Step 4: Wire the check into `verify_operator_session`**

In `src/skchat/operator_auth.py`, replace the existing revocation block inside `verify_operator_session`:

```python
    from .guest import _is_revoked  # reuse the guest revocation set

    if _is_revoked(claims["jti"]):
        raise OperatorAuthError("revoked")
    return OperatorSession(jti=claims["jti"], device_fp=claims["device_fp"], exp=claims["exp"])
```

with:

```python
    from .guest import _is_revoked, is_device_revoked  # reuse the guest revocation store

    if _is_revoked(claims["jti"]):
        raise OperatorAuthError("revoked")
    # Device-level kill: unlinking a device revokes its fingerprint once, which
    # invalidates every session it holds without needing to know their jtis.
    if is_device_revoked(claims["device_fp"]):
        raise OperatorAuthError("device revoked")
    return OperatorSession(jti=claims["jti"], device_fp=claims["device_fp"], exp=claims["exp"])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_revocation.py -q`
Expected: PASS, 5 passed

- [ ] **Step 6: Run the existing auth and guest suites for regressions**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/ -q -k "operator_auth or guest or dataplane"`
Expected: PASS, no new failures

- [ ] **Step 7: Commit**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git add src/skchat/guest.py src/skchat/operator_auth.py tests/test_device_revocation.py
git commit -m "feat(devices): revoke sessions by device_fp so one write kills them all"
```

---

### Task 3: DeviceStore gains list and remove

**Files:**
- Modify: `src/skchat/operator_auth.py:127-160` (`DeviceStore`)
- Test: `tests/test_device_store_admin.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `DeviceStore.list_fps() -> list[str]`, `DeviceStore.remove(device_fp: str) -> bool`, `DeviceStore.clear() -> int`.

The spec notes `DeviceStore` has "no list method, no delete method". Unlink needs both.

- [ ] **Step 1: Write the failing tests**

```python
"""DeviceStore admin surface: list and remove, needed by unlink and by reset."""

from __future__ import annotations

import base64
import json

from skchat.operator_auth import DeviceStore


def _pub(seed: str) -> str:
    return base64.b64encode(seed.encode().ljust(32, b"\0")).decode()


def test_list_fps_returns_every_enrolled_fingerprint(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    a = store.enroll(_pub("alpha"))
    b = store.enroll(_pub("bravo"))
    assert sorted(store.list_fps()) == sorted([a, b])


def test_remove_deletes_one_device_and_persists(tmp_path):
    path = tmp_path / "devices.json"
    store = DeviceStore(path)
    a = store.enroll(_pub("alpha"))
    b = store.enroll(_pub("bravo"))

    assert store.remove(a) is True
    assert store.is_enrolled(a) is False
    assert store.is_enrolled(b) is True
    # Persisted, not just in memory.
    assert list(json.loads(path.read_text()).keys()) == [b]


def test_remove_of_an_unknown_device_is_false_not_an_error(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    assert store.remove("nosuchfingerprint") is False


def test_clear_empties_the_store_and_reports_the_count(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    store.enroll(_pub("alpha"))
    store.enroll(_pub("bravo"))
    assert store.clear() == 2
    assert store.list_fps() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_store_admin.py -q`
Expected: FAIL, `AttributeError: 'DeviceStore' object has no attribute 'list_fps'`

- [ ] **Step 3: Add the methods**

In `src/skchat/operator_auth.py`, factor the atomic write out of `enroll` and add the three methods. Replace the body of `enroll` and append after `pubkey_for`:

```python
    def _write(self) -> None:
        """Atomic write of the current map (caller holds ``self._lock``)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file in the same directory, then os.replace()
        # onto the target, so a crash mid-write never leaves a torn file
        # (either the old contents are intact or the new ones are, never
        # a half-written mix).
        tmp = self._path.with_suffix(self._path.suffix + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(self._data))
        os.replace(tmp, self._path)

    def enroll(self, device_pubkey_b64: str) -> str:
        fp = device_fingerprint(device_pubkey_b64)
        with self._lock:
            self._data[fp] = device_pubkey_b64
            self._write()
        return fp

    def list_fps(self) -> list[str]:
        """Every enrolled device fingerprint."""
        with self._lock:
            return list(self._data.keys())

    def remove(self, device_fp: str) -> bool:
        """Drop a device so no NEW session can be minted for it."""
        with self._lock:
            if device_fp not in self._data:
                return False
            del self._data[device_fp]
            self._write()
            return True

    def clear(self) -> int:
        """Remove every enrolled device (the R1 clean cut). Returns the count."""
        with self._lock:
            count = len(self._data)
            self._data = {}
            self._write()
            return count
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_store_admin.py -q`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git add src/skchat/operator_auth.py tests/test_device_store_admin.py
git commit -m "feat(devices): DeviceStore list_fps, remove and clear"
```

---

### Task 4: Stash the verified session on the request (R3)

**Files:**
- Modify: `src/skchat/dataplane_auth.py:716-760` (`enforce_dataplane_auth`)
- Test: `tests/test_dataplane_session_stash.py` (create)

**Interfaces:**
- Consumes: `device_registry.touch` (Task 1).
- Produces: `request.state.operator_session` is an `OperatorSession` when the caller presented a valid operator session, and is absent otherwise. Task 5 reads it. Also adds `device_registry.touch_throttled(device_fp: str) -> bool` and `device_registry.TOUCH_THROTTLE_SECONDS` (Step 5).

Why this task exists: the spec assumed the publish handler "already has the session in hand". It does not. `require_dataplane_auth` is `async def ... -> None` and discards the verified session entirely.

- [ ] **Step 1: Write the failing tests**

```python
"""R3: the verified operator session must reach the route that needs device_fp."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from skchat import operator_auth as OA
from skchat.dataplane_auth import require_dataplane_auth


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "off")
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()

    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request, _auth: None = Depends(require_dataplane_auth)):
        session = getattr(request.state, "operator_session", None)
        return {"device_fp": getattr(session, "device_fp", None)}

    return TestClient(app)


def test_the_route_sees_the_device_fp_of_the_session_that_authenticated_it(client):
    token = OA.mint_operator_session(device_fp="a1b2c3d4e5f60718")
    r = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["device_fp"] == "a1b2c3d4e5f60718"


def test_two_devices_are_told_apart_by_their_own_sessions(client):
    one = OA.mint_operator_session(device_fp="aa" * 8)
    two = OA.mint_operator_session(device_fp="bb" * 8)
    first = client.get("/probe", headers={"Authorization": f"Bearer {one}"})
    second = client.get("/probe", headers={"Authorization": f"Bearer {two}"})
    assert first.json()["device_fp"] == "aa" * 8
    assert second.json()["device_fp"] == "bb" * 8


def test_with_the_gate_off_there_is_no_session_and_that_is_not_an_error(
    client, monkeypatch
):
    # Gate off is the default in dev/tests: no credential is verified, so no
    # session exists. Routes must treat this as "unknown device", not a failure.
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "")
    r = client.get("/probe")
    assert r.status_code == 200
    assert r.json()["device_fp"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_dataplane_session_stash.py -q`
Expected: FAIL, the first two tests assert `device_fp` but get `None`

- [ ] **Step 3: Add the stash helper and call it**

In `src/skchat/dataplane_auth.py`, add this function immediately above `enforce_dataplane_auth`:

```python
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
    except Exception:
        # Not an operator session (or an unverifiable one). Nothing to stash.
        pass
```

Then inside `enforce_dataplane_auth`, immediately after the `legacy_ok` assignment and before `mode = authz_pdp_mode()`:

```python
    if legacy_ok and token:
        _stash_operator_session(request, token)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_dataplane_session_stash.py -q`
Expected: PASS, 3 passed

- [ ] **Step 5: Bump `last_seen`, throttled**

The spec asks for `last_seen` to be "bumped on any authenticated request (cheap, best-effort)". A naive implementation is NOT cheap: the registry is a JSON file, so touching it per request means a full read-modify-atomic-write on every authenticated call. Throttle it so at most one write happens per device per minute.

Add to `device_registry.py`:

```python
#: Minimum seconds between two ``last_seen`` writes for the same device. The
#: registry is a whole-file JSON rewrite, so an unthrottled per-request touch
#: would be a write storm on a busy daemon. Minute resolution is far finer than
#: the "3 hours ago" the UI renders.
TOUCH_THROTTLE_SECONDS = 60

_last_touch: dict[str, float] = {}


def touch_throttled(device_fp: str) -> bool:
    """Bump ``last_seen`` at most once per :data:`TOUCH_THROTTLE_SECONDS`.

    Returns True if a write actually happened. Never raises: a failure to record
    liveness must not affect the request that triggered it.
    """
    if not device_fp:
        return False
    now = time.time()
    previous = _last_touch.get(device_fp, 0.0)
    if now - previous < TOUCH_THROTTLE_SECONDS:
        return False
    _last_touch[device_fp] = now
    try:
        touch(device_fp)
        return True
    except Exception:
        logger.debug("last_seen touch failed for %s (best-effort)", device_fp, exc_info=True)
        return False
```

Then call it from `_stash_operator_session` in `dataplane_auth.py`, immediately after the successful stash:

```python
        request.state.operator_session = verify_operator_session(token)
        from .device_registry import touch_throttled

        touch_throttled(request.state.operator_session.device_fp)
```

Add this test to `tests/test_device_registry.py`:

```python
def test_touch_throttled_writes_at_most_once_per_window(monkeypatch):
    DR.record_enroll("ab" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    DR._last_touch.clear()
    assert DR.touch_throttled("ab" * 8) is True
    assert DR.touch_throttled("ab" * 8) is False  # inside the window, no second write
    DR._last_touch["ab" * 8] = 0.0  # pretend the window elapsed
    assert DR.touch_throttled("ab" * 8) is True


def test_touch_throttled_on_an_unknown_device_is_harmless():
    DR._last_touch.clear()
    assert DR.touch_throttled("99" * 8) is False or DR.get_device("99" * 8) is None
```

- [ ] **Step 6: Run the dataplane and registry suites for regressions**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/ -q -k "dataplane or operator or device_registry"`
Expected: PASS, no new failures

- [ ] **Step 7: Commit**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git add src/skchat/dataplane_auth.py src/skchat/device_registry.py tests/test_dataplane_session_stash.py tests/test_device_registry.py
git commit -m "feat(devices): stash the verified operator session and bump last_seen"
```

---

### Task 5: Record enrollment and publish into the registry

**Files:**
- Modify: `src/skchat/operator_auth_routes.py:38-61` (the `enroll` route)
- Modify: `src/skchat/daemon_proxy.py:1160-1192` (`api_publish_prekey`)
- Test: `tests/test_device_registry_wiring.py` (create)

**Interfaces:**
- Consumes: `device_registry.record_enroll`, `device_registry.record_publish` (Task 1); `request.state.operator_session` (Task 4); `guest.unrevoke_device` (Task 2).
- Produces: a registry row per enrolled device, with `key_ids` populated by publishes.

Note the subtlety: `pq_prekeys` derives the real slot filename through `_safe_slot_id(bundle["key_id"])`, so a bundle's claimed `key_id` is sanitized before it becomes a slot. Record the **sanitized** value, or unlink will look for a slot file that does not exist.

- [ ] **Step 1: Write the failing tests**

```python
"""The registry is populated by the two real routes, not by hand."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import device_registry as DR
from skchat import operator_auth as OA


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "off")
    monkeypatch.setenv("SKCHAT_PQC_DIR", str(tmp_path / "pqc"))
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    return tmp_path


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def test_publish_attributes_the_slot_to_the_session_that_published_it(client):
    fp = "a1b2c3d4e5f60718"
    DR.record_enroll(fp, label="L", label_source="derived", platform="web", user_agent="UA")
    token = OA.mint_operator_session(device_fp=fp)

    r = client.post(
        "/v1/prekey",
        json={
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "f8342853f762fd88" + "00" * 8,
            "key_id": "f8342853f762fd88",
            "owner": "chef",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert DR.get_device(fp)["key_ids"] == ["f8342853f762fd88"]


def test_two_devices_publishing_land_on_their_own_registry_rows(client):
    one, two = "aa" * 8, "bb" * 8
    for fp in (one, two):
        DR.record_enroll(fp, label="L", label_source="derived", platform="web", user_agent="UA")

    for fp, key_id in ((one, "1111111111111111"), (two, "2222222222222222")):
        token = OA.mint_operator_session(device_fp=fp)
        r = client.post(
            "/v1/prekey",
            json={
                "suite": "x25519-mlkem768",
                "hybrid_public_hex": key_id + "00" * 8,
                "key_id": key_id,
                "owner": "chef",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text

    assert DR.get_device(one)["key_ids"] == ["1111111111111111"]
    assert DR.get_device(two)["key_ids"] == ["2222222222222222"]


def test_a_publish_with_no_session_still_succeeds_and_records_nothing(client, monkeypatch):
    # Gate off: no session to attribute. The publish must still work.
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "")
    r = client.post(
        "/v1/prekey",
        json={
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "cc" * 16,
            "key_id": "cccccccccccccccc",
            "owner": "chef",
        },
    )
    assert r.status_code == 200, r.text
    assert DR.list_devices() == []


def test_the_recorded_key_id_is_the_sanitized_slot_id_not_the_raw_claim(client):
    # pq_prekeys sanitizes key_id into the slot filename. If the registry stored
    # the raw claim, unlink would hunt for a slot file that does not exist.
    fp = "dd" * 8
    DR.record_enroll(fp, label="L", label_source="derived", platform="web", user_agent="UA")
    token = OA.mint_operator_session(device_fp=fp)
    r = client.post(
        "/v1/prekey",
        json={
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "ee" * 16,
            "key_id": "../../escape/me",
            "owner": "chef",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    recorded = DR.get_device(fp)["key_ids"]
    assert recorded == ["escapeme"]
    assert ".." not in recorded[0] and "/" not in recorded[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_registry_wiring.py -q`
Expected: FAIL, `assert [] == ['f8342853f762fd88']`

- [ ] **Step 3: Record the publish**

In `src/skchat/daemon_proxy.py`, inside `api_publish_prekey`, replace the final return:

```python
    if not PQ.store_app_prekey_bundle(owner, body, signer_public_armor=signer):
        raise HTTPException(400, "prekey bundle rejected: unsigned or invalid signature")
    return JSONResponse({"ok": True, "stored": owner, "hybrid": PQ.peer_is_hybrid(owner)})
```

with:

```python
    if not PQ.store_app_prekey_bundle(owner, body, signer_public_armor=signer):
        raise HTTPException(400, "prekey bundle rejected: unsigned or invalid signature")
    _record_prekey_publish(request, body)
    return JSONResponse({"ok": True, "stored": owner, "hybrid": PQ.peer_is_hybrid(owner)})
```

and add this helper directly above `api_publish_prekey`:

```python
def _record_prekey_publish(request: Request, bundle: dict) -> None:
    """Attribute a stored prekey slot to the device whose session published it.

    This is the join that makes "unlink this device" able to find the device's
    prekey slots. Records the SANITIZED slot id (what ``pq_prekeys`` actually
    named the file), never the raw claimed ``key_id``, so unlink looks for a slot
    that exists.

    Best-effort by design: with the auth gate off there is no verified session,
    and a device enrolled before the registry existed has no row. Neither case is
    an error, and neither may break the publish.
    """
    try:
        session = getattr(request.state, "operator_session", None)
        device_fp = getattr(session, "device_fp", "")
        if not device_fp:
            return
        from skchat import device_registry as DR
        from skchat import pq_prekeys as PQ

        DR.record_publish(device_fp, PQ._safe_slot_id(bundle.get("key_id")))
    except Exception:
        logger.debug("prekey publish registry record failed (best-effort)", exc_info=True)
```

- [ ] **Step 4: Record the enrollment**

In `src/skchat/operator_auth_routes.py`, in the `enroll` route, replace:

```python
        grant_operator_prekey_capability(device_fp, pub)
        return {"device_fp": device_fp}
```

with:

```python
        grant_operator_prekey_capability(device_fp, pub)
        _record_enrollment(request, device_fp, label=body.get("label"))
        return {"device_fp": device_fp}
```

and add these helpers at module level (below `_canon`):

```python
def _derive_label(user_agent: str) -> tuple[str, str]:
    """Best-effort ``(label, platform)`` from a User-Agent string.

    Only used when the client sent no label of its own. Deliberately crude: it
    exists so a label-less enroll still shows something recognisable, not to be a
    UA parser. A native Dart client collapses to "App device" here, which is
    exactly why R2 has the client send its own label.
    """
    ua = (user_agent or "").strip()
    if not ua:
        return "Unknown device", "unknown"
    lowered = ua.lower()
    for needle, name, platform in (
        ("firefox", "Firefox", "web"),
        ("edg/", "Edge", "web"),
        ("chrome", "Chrome", "web"),
        ("safari", "Safari", "web"),
        ("dart", "App device", "app"),
    ):
        if needle in lowered:
            return name, platform
    return ua[:40], "unknown"


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
```

Add `import logging` to the module imports.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_registry_wiring.py -q`
Expected: PASS, 4 passed

- [ ] **Step 6: Commit**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git add src/skchat/daemon_proxy.py src/skchat/operator_auth_routes.py tests/test_device_registry_wiring.py
git commit -m "feat(devices): record enrollment and prekey publish into the registry"
```

---

### Task 6: The signed enrollment label (R2)

**Files:**
- Modify: `src/skchat/operator_auth_routes.py:38-61` (the `enroll` route signature check)
- Test: `tests/test_operator_enroll_label.py` (create)

**Interfaces:**
- Consumes: `_record_enrollment` (Task 5).
- Produces: `POST /api/v1/auth/enroll` accepts an optional `label`; when present the signature must cover `{"label": ..., "nonce": ..., "device_pubkey": ...}`.

The signed payload is built with `_canon`, which sorts keys, so the canonical bytes for a labelled enroll are `{"device_pubkey":...,"label":...,"nonce":...}`. The client must sort keys identically.

- [ ] **Step 1: Write the failing tests**

```python
"""R2: an enrollment label is bound into the device signature or it is not accepted."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import device_registry as DR
from skchat import operator_auth as oa
from skchat.operator_auth_routes import register_operator_auth_routes


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def pub_b64(key):
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(raw).decode()


def _sign(key, payload: bytes) -> str:
    der = key.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    app = FastAPI()
    register_operator_auth_routes(app, device_store=oa.DeviceStore(tmp_path / "devices.json"))
    return TestClient(app)


def _open_window(client) -> str:
    r = client.post("/api/v1/auth/enroll/open")
    assert r.status_code == 200, r.text
    return r.json()["window_nonce"]


def test_a_signed_label_is_accepted_and_stored_as_client_sourced(client, key, pub_b64):
    nonce = _open_window(client)
    payload = _canon({"nonce": nonce, "device_pubkey": pub_b64, "label": "Chef's Pixel"})
    r = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub_b64,
            "window_nonce": nonce,
            "sig": _sign(key, payload),
            "label": "Chef's Pixel",
        },
    )
    assert r.status_code == 200, r.text
    row = DR.get_device(r.json()["device_fp"])
    assert row["label"] == "Chef's Pixel"
    assert row["label_source"] == "client"


def test_a_label_not_covered_by_the_signature_is_rejected(client, key, pub_b64):
    # Signature over the OLD two-field payload, but a label rides along in the
    # body. Accepting this would let a proxy write any label it liked.
    nonce = _open_window(client)
    payload = _canon({"nonce": nonce, "device_pubkey": pub_b64})
    r = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub_b64,
            "window_nonce": nonce,
            "sig": _sign(key, payload),
            "label": "Injected",
        },
    )
    assert r.status_code == 401


def test_a_tampered_label_is_rejected(client, key, pub_b64):
    nonce = _open_window(client)
    payload = _canon({"nonce": nonce, "device_pubkey": pub_b64, "label": "Real"})
    r = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub_b64,
            "window_nonce": nonce,
            "sig": _sign(key, payload),
            "label": "Tampered",
        },
    )
    assert r.status_code == 401


def test_an_enroll_with_no_label_still_works_on_the_old_payload(client, key, pub_b64):
    # Backwards compatibility: the shipped web build signs two fields only.
    nonce = _open_window(client)
    payload = _canon({"nonce": nonce, "device_pubkey": pub_b64})
    r = client.post(
        "/api/v1/auth/enroll",
        json={"device_pubkey": pub_b64, "window_nonce": nonce, "sig": _sign(key, payload)},
        headers={"User-Agent": "Mozilla/5.0 Chrome/131 Safari/537.36"},
    )
    assert r.status_code == 200, r.text
    row = DR.get_device(r.json()["device_fp"])
    assert row["label_source"] == "derived"
    assert row["label"] == "Chrome"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_operator_enroll_label.py -q`
Expected: FAIL, the label tests get 401/200 mismatches because `label` is not part of the signed payload yet

- [ ] **Step 3: Bind the label into the verified payload**

In the `enroll` route, replace:

```python
        if not oa.verify_device_signature(
            device_pubkey_b64=pub,
            payload=_canon({"nonce": wnonce, "device_pubkey": pub}),
            sig_b64=sig,
        ):
            raise HTTPException(401, "device signature invalid")
```

with:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_operator_enroll_label.py -q`
Expected: PASS, 4 passed

- [ ] **Step 5: Run the existing enroll suite for regressions**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/ -q -k "enroll or operator_auth"`
Expected: PASS, no new failures

- [ ] **Step 6: Commit**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git add src/skchat/operator_auth_routes.py tests/test_operator_enroll_label.py
git commit -m "feat(devices): bind the enrollment label into the device signature"
```

---

### Task 7: The unlink orchestration

**Files:**
- Create: `src/skchat/device_unlink.py`
- Test: `tests/test_device_unlink.py`

**Interfaces:**
- Consumes: `device_registry` (Task 1), `guest.revoke_device` (Task 2), `DeviceStore.remove` (Task 3).
- Produces: `unlink_device(device_fp: str, *, device_store, owner: str = "chef") -> dict` returning `{"device_fp", "sessions_revoked", "slots_removed", "store_removed", "capauth_revoked", "registry_marked"}`.

This is the security crux of the epic. The final test here is the one the whole epic exists for.

- [ ] **Step 1: Write the failing tests**

```python
"""Unlink: the security crux. A partial unlink is a silent hole, so prove each store."""

from __future__ import annotations

import base64

import pytest

from skchat import device_registry as DR
from skchat import device_unlink as DU
from skchat import operator_auth as OA
from skchat import pq_prekeys as PQ


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_PQC_DIR", str(tmp_path / "pqc"))
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return OA.DeviceStore(tmp_path / "devices.json")


def _enrol(store, seed: str, key_id: str) -> str:
    """Enrol a device, register it, and give it a published prekey slot."""
    pub = base64.b64encode(seed.encode().ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label=seed, label_source="client", platform="app", user_agent="UA")
    PQ.store_peer_bundle(
        "chef",
        {"suite": "x25519-mlkem768", "hybrid_public_hex": key_id + "00" * 8, "key_id": key_id},
    )
    DR.record_publish(fp, key_id)
    return fp


def test_unlink_revokes_sessions_drops_the_slot_and_deletes_the_device(store):
    fp = _enrol(store, "alpha", "1111111111111111")
    token = OA.mint_operator_session(device_fp=fp)
    assert OA.verify_operator_session(token).device_fp == fp

    result = DU.unlink_device(fp, device_store=store)

    assert result["sessions_revoked"] is True
    assert result["slots_removed"] == ["1111111111111111"]
    assert result["store_removed"] is True
    assert result["registry_marked"] is True
    # 1. sessions dead
    with pytest.raises(OA.OperatorAuthError):
        OA.verify_operator_session(token)
    # 2. no new session can be minted (the key is gone from the store)
    assert store.is_enrolled(fp) is False
    # 3. the KEM slot is gone, so fanout cannot reach it
    assert [b["key_id"] for b in PQ.load_peer_bundles("chef")] == []
    # 4. the row is kept for audit but hidden
    assert DR.list_devices() == []
    assert len(DR.list_devices(include_revoked=True)) == 1


def test_unlink_is_idempotent(store):
    fp = _enrol(store, "alpha", "1111111111111111")
    first = DU.unlink_device(fp, device_store=store)
    second = DU.unlink_device(fp, device_store=store)
    assert first["store_removed"] is True
    assert second["store_removed"] is False  # already gone
    assert second["sessions_revoked"] is True  # revocation stays asserted


def test_unlink_of_an_unknown_device_raises_key_error(store):
    with pytest.raises(KeyError):
        DU.unlink_device("nosuchdevice", device_store=store)


def test_unlinking_one_device_does_not_disturb_another(store):
    keep = _enrol(store, "keeper", "1111111111111111")
    drop = _enrol(store, "dropme", "2222222222222222")
    keep_token = OA.mint_operator_session(device_fp=keep)

    DU.unlink_device(drop, device_store=store)

    assert OA.verify_operator_session(keep_token).device_fp == keep
    assert store.is_enrolled(keep) is True
    assert [b["key_id"] for b in PQ.load_peer_bundles("chef")] == ["1111111111111111"]


def test_the_whole_point_fanout_reaches_the_survivor_only(store):
    """THE assertion this epic exists for.

    Two devices, both publishing. Unlink one. A fresh fanout must seal to the
    survivor's slot ONLY, and the unlinked device's slot file must be gone from
    disk. This fails if ANY single step of unlink is skipped, which is exactly
    the "partial unlink is a silent security hole" the design warns about.
    """
    survivor = _enrol(store, "survivor", "1111111111111111")
    unlinked = _enrol(store, "unlinked", "2222222222222222")
    assert sorted(b["key_id"] for b in PQ.load_peer_bundles("chef")) == [
        "1111111111111111",
        "2222222222222222",
    ]

    DU.unlink_device(unlinked, device_store=store)

    # The fanout target list is exactly the survivor.
    targets = [b["key_id"] for b in PQ.load_peer_bundles("chef")]
    assert targets == ["1111111111111111"]
    # And the slot file itself is gone from disk, not merely filtered.
    assert not (PQ._peer_dir("chef") / "2222222222222222.json").exists()
    # The survivor is untouched and can still authenticate.
    assert store.is_enrolled(survivor) is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_unlink.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'skchat.device_unlink'`

- [ ] **Step 3: Write the implementation**

```python
"""Unlink a linked device across every store that can still reach it.

The security crux of "Linked Devices". A device is present in four places, and
leaving it in ANY of them is a silent hole:

  1. ``revoked_device_fps``   - not there means its live sessions keep working.
  2. prekey slots             - not removed means fanout keeps SEALING NEW
                                MESSAGES the device can still decrypt, which is
                                the worst of the four because it is invisible.
  3. ``DeviceStore``          - not removed means it can mint a brand new session.
  4. capauth pairing record   - not revoked means the PDP still grants it
                                capabilities.

Order matters: sessions die FIRST, so nothing the device does during the rest of
the unlink is authorized. Every step is independently retry-safe, and a partial
failure leaves the device MORE locked out, never less. Steps 4 and 5 are
best-effort and never raise, because a capauth hiccup must not leave the first
three steps unreported.

Prekey removal calls :func:`skchat.pq_prekeys.remove_peer_bundle`, the SAME
primitive behind ``DELETE /v1/prekey/{peer}/{key_id}``, so the multi-device
revoke path and this unlink path can never diverge (spec R4).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("skchat.device_unlink")


def unlink_device(device_fp: str, *, device_store, owner: str = "chef") -> dict:
    """Revoke *device_fp* everywhere. Returns a per-step report.

    Args:
        device_fp: The device to unlink.
        device_store: The live :class:`skchat.operator_auth.DeviceStore`.
        owner: Short name whose prekey slots hold this device (default ``chef``).

    Raises:
        KeyError: if the fingerprint is in neither the registry nor the store.
    """
    from skchat import device_registry as DR
    from skchat import pq_prekeys as PQ
    from skchat.guest import revoke_device

    row = DR.get_device(device_fp)
    if row is None and not device_store.is_enrolled(device_fp):
        raise KeyError(device_fp)

    # 1. Kill every session this device holds, before anything else.
    revoke_device(device_fp)

    # 2. Drop its prekey slots so the next fanout cannot seal to it.
    slots_removed: list[str] = []
    for key_id in list((row or {}).get("key_ids") or []):
        try:
            if PQ.remove_peer_bundle(owner, key_id):
                slots_removed.append(key_id)
        except Exception:
            logger.warning("unlink: prekey slot %s not removed for %s", key_id, device_fp)

    # 3. Remove the auth key so no NEW session can be minted.
    store_removed = device_store.remove(device_fp)

    # 4. Best-effort: revoke the capauth pairing record behind the PDP grant.
    capauth_revoked = _revoke_capauth_subject(device_fp)

    # 5. Keep the row for audit, hidden from the default list.
    registry_marked = DR.mark_revoked(device_fp)

    logger.info(
        "unlinked device %s (slots=%d store=%s capauth=%s)",
        device_fp,
        len(slots_removed),
        store_removed,
        capauth_revoked,
    )
    return {
        "device_fp": device_fp,
        "sessions_revoked": True,
        "slots_removed": slots_removed,
        "store_removed": store_removed,
        "capauth_revoked": capauth_revoked,
        "registry_marked": registry_marked,
    }


def _revoke_capauth_subject(device_fp: str) -> bool:
    """Revoke the ``operator:<fp>`` pairing record. Never raises.

    Mirrors the best-effort posture of :mod:`skchat.operator_grants`: capauth is
    optional at runtime, and a failure here must not stop the caller from
    learning that steps 1 to 3 succeeded.
    """
    try:
        from capauth import pairing

        from skchat.dataplane_auth import operator_subject

        pairing.revoke(operator_subject(device_fp))
        return True
    except Exception:
        logger.debug("capauth revoke unavailable for %s (best-effort)", device_fp, exc_info=True)
        return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_unlink.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Verify the capauth revoke signature really exists**

Run: `cd ~ && ~/.skenv/bin/python -c "from capauth import pairing; print(pairing.revoke)"`
Expected: prints a function. If it raises `ImportError` or `AttributeError`, the best-effort wrapper still returns False and the tests still pass, but note the real name in `_revoke_capauth_subject` and adjust the call.

- [ ] **Step 6: Commit**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git add src/skchat/device_unlink.py tests/test_device_unlink.py
git commit -m "feat(devices): unlink a device across sessions, prekeys, store and capauth"
```

---

### Task 8: The three operator endpoints (plus the PDP capability mapping)

**Files:**
- Create: `src/skchat/device_routes.py`
- Modify: `src/skchat/dataplane_auth.py` (`_ROUTE_CAPABILITY_RULES`)
- Modify: `src/skchat/webui.py` (register the router; find the call site with `grep -n "register_operator_auth_routes" src/skchat/webui.py`)
- Test: `tests/test_device_routes.py`

**Interfaces:**
- Consumes: `device_registry.list_devices` (Task 1), `device_unlink.unlink_device` (Task 7), `guest._require_operator`.
- Produces:
  - `GET /api/v1/operator/devices` -> `{"devices": [{device_fp, label, label_source, platform, enrolled_at, last_seen, key_ids, is_current}]}`
  - `DELETE /api/v1/operator/devices/{device_fp}` -> the unlink report, 400 on self, 404 on unknown
  - `POST /api/v1/operator/devices/unlink-others` -> `{"unlinked": [fp, ...]}`
  - `register_device_routes(app: FastAPI, *, device_store) -> None`

The capability mapping is not optional: live runs `SKCHAT_AUTHZ_PDP=enforce`, where an unmapped gated route fails closed, and `tests/test_dataplane_coverage.py` fails CI for unmapped routes. Map to `CAP_PREKEY`, which enrolled devices already hold via `grant_operator_prekey_capability`, so no new grant is needed for an existing device to manage devices.

**Deliberate deviation from the spec:** the spec writes the list response as a bare array. This plan wraps it as `{"devices": [...]}` to match how every other skchat JSON route replies (an object envelope), which leaves room to add fields later without breaking the client. The app in Task 11 reads the envelope.

- [ ] **Step 1: Write the failing tests**

```python
"""The three Linked Devices endpoints, driven through a real FastAPI app."""

from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import device_registry as DR
from skchat import operator_auth as OA
from skchat import pq_prekeys as PQ
from skchat.device_routes import register_device_routes


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-secret")
    monkeypatch.setenv("SKCHAT_PQC_DIR", str(tmp_path / "pqc"))
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    return OA.DeviceStore(tmp_path / "devices.json")


@pytest.fixture
def client(store):
    app = FastAPI()
    register_device_routes(app, device_store=store)
    return TestClient(app)


def _enrol(store, seed: str, key_id: str) -> str:
    pub = base64.b64encode(seed.encode().ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label=seed, label_source="client", platform="app", user_agent="UA")
    PQ.store_peer_bundle(
        "chef",
        {"suite": "x25519-mlkem768", "hybrid_public_hex": key_id + "00" * 8, "key_id": key_id},
    )
    DR.record_publish(fp, key_id)
    return fp


def _as(fp: str) -> dict:
    return {"Authorization": f"Bearer {OA.mint_operator_session(device_fp=fp)}"}


def test_list_marks_the_calling_device_as_current(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    other = _enrol(store, "otherdev", "2222222222222222")

    r = client.get("/api/v1/operator/devices", headers=_as(me))
    assert r.status_code == 200, r.text
    rows = {d["device_fp"]: d for d in r.json()["devices"]}
    assert rows[me]["is_current"] is True
    assert rows[other]["is_current"] is False
    assert rows[me]["label"] == "mydevice"
    assert rows[other]["key_ids"] == ["2222222222222222"]


def test_a_non_operator_is_refused(client, store):
    _enrol(store, "mydevice", "1111111111111111")
    assert client.get("/api/v1/operator/devices").status_code in (401, 403)


def test_unlink_removes_the_other_device_and_its_slot(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    other = _enrol(store, "otherdev", "2222222222222222")

    r = client.delete(f"/api/v1/operator/devices/{other}", headers=_as(me))
    assert r.status_code == 200, r.text
    assert r.json()["slots_removed"] == ["2222222222222222"]
    assert [b["key_id"] for b in PQ.load_peer_bundles("chef")] == ["1111111111111111"]
    assert store.is_enrolled(other) is False


def test_a_device_cannot_unlink_itself(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    r = client.delete(f"/api/v1/operator/devices/{me}", headers=_as(me))
    assert r.status_code == 400
    assert store.is_enrolled(me) is True  # nothing happened


def test_unlink_of_an_unknown_device_is_404(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    r = client.delete("/api/v1/operator/devices/deadbeefdeadbeef", headers=_as(me))
    assert r.status_code == 404


def test_unlink_others_spares_the_caller(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    a = _enrol(store, "deviceaa", "2222222222222222")
    b = _enrol(store, "devicebb", "3333333333333333")

    r = client.post("/api/v1/operator/devices/unlink-others", headers=_as(me))
    assert r.status_code == 200, r.text
    assert sorted(r.json()["unlinked"]) == sorted([a, b])
    assert store.is_enrolled(me) is True
    assert store.is_enrolled(a) is False and store.is_enrolled(b) is False
    assert [d["device_fp"] for d in DR.list_devices()] == [me]


def test_every_new_route_is_capability_mapped_for_the_enforcing_pdp():
    # Live runs SKCHAT_AUTHZ_PDP=enforce, where an unmapped gated route fails
    # closed, and tests/test_dataplane_coverage.py fails CI for one.
    from skchat.dataplane_auth import CAP_PREKEY, route_capability

    assert route_capability("GET", "/api/v1/operator/devices") == CAP_PREKEY
    assert route_capability("DELETE", "/api/v1/operator/devices/abc123") == CAP_PREKEY
    assert route_capability("POST", "/api/v1/operator/devices/unlink-others") == CAP_PREKEY
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_routes.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'skchat.device_routes'`

- [ ] **Step 3: Write the routes**

```python
"""Operator endpoints for the "Linked Devices" surface.

Thin by design: parse, authorize, delegate. The list comes from
:mod:`skchat.device_registry` and the revocation work is
:func:`skchat.device_unlink.unlink_device`; nothing security-relevant is decided
here.

Authorization reuses ``guest._require_operator``, which already accepts EITHER
the shared operator token OR an enrolled-operator session Bearer, so the app's
existing auth interceptor works unchanged.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("skchat.device_routes")


def _current_device_fp(request: Request) -> str:
    """The fingerprint of the device making this call, or "" if unknown.

    Read from the caller's own operator session. A caller presenting the shared
    operator token instead of a session has no device identity, which simply
    means no row is marked current and self-unlink cannot be detected. That is
    why unlink-others refuses to run without a known current device.
    """
    session = getattr(request.state, "operator_session", None)
    if session is not None and getattr(session, "device_fp", ""):
        return session.device_fp
    auth = (request.headers.get("authorization") or "").strip()
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        return ""
    try:
        from skchat.operator_auth import verify_operator_session

        return verify_operator_session(token).device_fp
    except Exception:
        return ""


def register_device_routes(app: FastAPI, *, device_store) -> None:
    router = APIRouter(prefix="/api/v1/operator")

    @router.get("/devices")
    async def list_devices(request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_registry as DR

        current = _current_device_fp(request)
        rows = []
        for row in DR.list_devices():
            item = dict(row)
            item.pop("user_agent", None)  # internal detail, not UI data
            item["is_current"] = bool(current) and row["device_fp"] == current
            rows.append(item)
        return JSONResponse({"devices": rows})

    @router.delete("/devices/{device_fp}")
    async def unlink(device_fp: str, request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_unlink as DU

        if device_fp == _current_device_fp(request):
            raise HTTPException(
                400,
                "cannot unlink the device you are using; unlink it from another "
                "device, or use unlink-others",
            )
        try:
            return JSONResponse(DU.unlink_device(device_fp, device_store=device_store))
        except KeyError:
            raise HTTPException(404, "device not found")

    @router.post("/devices/unlink-others")
    async def unlink_others(request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_registry as DR
        from skchat import device_unlink as DU

        current = _current_device_fp(request)
        if not current:
            raise HTTPException(
                400, "unlink-others requires an operator session so this device can be spared"
            )
        unlinked = []
        for row in DR.list_devices():
            fp = row["device_fp"]
            if fp == current:
                continue
            try:
                DU.unlink_device(fp, device_store=device_store)
                unlinked.append(fp)
            except KeyError:
                continue
        return JSONResponse({"unlinked": unlinked})

    app.include_router(router)
```

- [ ] **Step 4: Add the capability mapping**

In `src/skchat/dataplane_auth.py`, add to `_ROUTE_CAPABILITY_RULES` (in the `skchat.prekey` section, so the grouping comment stays true):

```python
    # Linked Devices: managing a device means managing its prekey slots, and an
    # enrolled device already holds skchat.prekey, so no new grant is needed.
    ("GET", "/api/v1/operator/devices", CAP_PREKEY),
    ("DELETE", "/api/v1/operator/devices/{device_fp}", CAP_PREKEY),
    ("POST", "/api/v1/operator/devices/unlink-others", CAP_PREKEY),
```

- [ ] **Step 5: Register the router**

Find where the operator auth routes are registered and add the device routes beside them:

Run: `cd /home/cbrd21/skworld-worktrees/s3-devmgmt && grep -n "register_operator_auth_routes" src/skchat/webui.py`

Then add, immediately after that call, using the same `device_store` object already in scope there:

```python
    from .device_routes import register_device_routes

    register_device_routes(app, device_store=device_store)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_device_routes.py -q`
Expected: PASS, 7 passed

- [ ] **Step 7: Run the PDP coverage gate**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_dataplane_coverage.py -q`
Expected: PASS. If it fails naming your new routes, they are gated but unmapped: re-check Step 4.

- [ ] **Step 8: Commit**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git add src/skchat/device_routes.py src/skchat/dataplane_auth.py src/skchat/webui.py tests/test_device_routes.py
git commit -m "feat(devices): list and unlink endpoints for Linked Devices"
```

---

### Task 9: The clean-cut CLI (R1)

**Files:**
- Modify: `src/skchat/cli.py`
- Test: `tests/test_devices_reset_cli.py`

**Interfaces:**
- Consumes: `device_registry.clear_all` (Task 1), `DeviceStore.clear` (Task 3).
- Produces: `skchat devices reset --yes`.

Why a command and not a migration: the live box holds 13 enrolled devices and 6 prekey slots with no correlating data, so their `key_ids` are unknowable and unlink would be silently partial for all of them. Wiping automatically on upgrade would lock out every device the moment the code deploys, so the operator triggers it.

- [ ] **Step 1: Write the failing tests**

```python
"""The R1 clean cut: an operator-triggered reset, never an automatic one."""

from __future__ import annotations

import base64

import pytest
from click.testing import CliRunner

from skchat import device_registry as DR
from skchat import operator_auth as OA
from skchat import pq_prekeys as PQ
from skchat.cli import cli


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_PQC_DIR", str(tmp_path / "pqc"))
    monkeypatch.setenv("SKCHAT_OPERATOR_DEVICES", str(tmp_path / "devices.json"))
    store = OA.DeviceStore(tmp_path / "devices.json")
    pub = base64.b64encode(b"alpha".ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label="L", label_source="client", platform="app", user_agent="UA")
    PQ.store_peer_bundle(
        "chef",
        {"suite": "x25519-mlkem768", "hybrid_public_hex": "aa" * 16, "key_id": "aaaaaaaaaaaaaaaa"},
    )
    DR.record_publish(fp, "aaaaaaaaaaaaaaaa")
    return tmp_path


def test_reset_without_yes_refuses_and_changes_nothing():
    result = CliRunner().invoke(cli, ["devices", "reset"])
    assert result.exit_code != 0
    assert DR.list_devices(include_revoked=True) != []
    assert PQ.load_peer_bundles("chef") != []


def test_reset_with_yes_clears_every_store():
    result = CliRunner().invoke(cli, ["devices", "reset", "--yes"])
    assert result.exit_code == 0, result.output
    assert DR.list_devices(include_revoked=True) == []
    assert PQ.load_peer_bundles("chef") == []
    assert OA.DeviceStore(__import__("os").environ["SKCHAT_OPERATOR_DEVICES"]).list_fps() == []


def test_reset_reports_what_it_removed():
    result = CliRunner().invoke(cli, ["devices", "reset", "--yes"])
    assert "1" in result.output  # counts are surfaced, not silent
    assert "device" in result.output.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_devices_reset_cli.py -q`
Expected: FAIL, `No such command 'devices'`

- [ ] **Step 3: Confirm the DeviceStore path env var name**

The test assumes `SKCHAT_OPERATOR_DEVICES` names the store path.

Run: `cd /home/cbrd21/skworld-worktrees/s3-devmgmt && grep -rn "operator_devices.json" src/skchat/ | grep -v "\.pyc"`

If a different env var (or a hardcoded path) is used, use that name in both the CLI and the test. Do not invent one.

- [ ] **Step 4: Add the command group to `cli.py`**

```python
@cli.group()
def devices():
    """Manage linked operator devices."""


@devices.command("reset")
@click.option("--yes", is_flag=True, help="Confirm: this unlinks every device.")
def devices_reset(yes: bool):
    """Clear every enrolled device, its prekey slots, and the registry.

    The clean cut. A device enrolled before the registry existed has no recorded
    prekey slots, so unlinking it would be silently partial: its sessions would
    die while its KEM slot stayed live and kept receiving mail. Rather than carry
    that ambiguity forever, wipe once and re-link. Deliberately manual, because
    doing it automatically on upgrade would lock out every device the moment the
    new code deployed.
    """
    import os

    from skchat import device_registry as DR
    from skchat import pq_prekeys as PQ
    from skchat.operator_auth import DeviceStore

    if not yes:
        raise click.ClickException(
            "This unlinks EVERY device and they must all re-link. Re-run with --yes."
        )

    store_path = os.getenv("SKCHAT_OPERATOR_DEVICES", "").strip() or os.path.expanduser(
        "~/.skchat/state/operator_devices.json"
    )
    store = DeviceStore(store_path)
    device_count = store.clear()

    slot_count = 0
    for bundle in PQ.load_peer_bundles("chef"):
        key_id = bundle.get("key_id")
        if key_id and PQ.remove_peer_bundle("chef", key_id):
            slot_count += 1

    registry_count = DR.clear_all()

    click.echo(
        f"Cleared {device_count} enrolled device(s), {slot_count} prekey slot(s), "
        f"{registry_count} registry row(s)."
    )
    click.echo("Re-link each device you still use from its own app.")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/test_devices_reset_cli.py -q`
Expected: PASS, 3 passed

- [ ] **Step 6: Commit**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git add src/skchat/cli.py tests/test_devices_reset_cli.py
git commit -m "feat(devices): skchat devices reset, the operator-triggered clean cut"
```

---

### Task 10: Server verification and PR

**Files:** none changed (verification only).

- [ ] **Step 1: Run the full suite**

Run: `cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/ -q 2>&1 | tail -30`

- [ ] **Step 2: Prove no NEW failures against main**

skchat main is chronically red on pre-existing environment failures (e2e-live transport, systemd units, telegram model swap). Counting is not enough; diff the failing NAMES.

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/ -q 2>&1 \
  | grep -E "^FAILED" | sed 's/ - .*//' | sort > /tmp/branch_fails.txt
cd /home/cbrd21/skworld-worktrees/s3-devmgmt && git stash push -q -u && git checkout -q origin/main -- .
cd ~ && ~/.skenv/bin/python -m pytest /home/cbrd21/skworld-worktrees/s3-devmgmt/tests/ -q 2>&1 \
  | grep -E "^FAILED" | sed 's/ - .*//' | sort > /tmp/main_fails.txt
cd /home/cbrd21/skworld-worktrees/s3-devmgmt && git checkout -q HEAD -- . && git stash pop -q
diff /tmp/main_fails.txt /tmp/branch_fails.txt && echo "IDENTICAL FAILING SET"
```

Expected: "IDENTICAL FAILING SET". Anything appearing only in `branch_fails.txt` is yours to fix.

- [ ] **Step 3: Lint everything touched**

Run: `cd /home/cbrd21/skworld-worktrees/s3-devmgmt && ~/.skenv/bin/ruff check src/ tests/ && ~/.skenv/bin/ruff format --check --line-length 99 $(git diff --name-only origin/main -- '*.py')`

Note: `ruff check src/ tests/` reports 9 errors on main already (1 F401, 1 F811, 7 I001). Your job is that the count and identity do not grow.

- [ ] **Step 4: Confirm the house rule**

Run: `cd /home/cbrd21/skworld-worktrees/s3-devmgmt && git diff origin/main | grep -nP "^\+.*[—–]" && echo "FOUND DASHES, fix them" || echo "clean"`
Expected: "clean"

- [ ] **Step 5: Push and open the PR**

```bash
cd /home/cbrd21/skworld-worktrees/s3-devmgmt
git push -u origin feat/device-management
gh pr create --base main --title "feat(devices): Linked Devices phase 1, list and unlink"
```

The PR body must state the failing-set diff result from Step 2 explicitly, the same bar used for PR #116.

---

### Task 11 (app, on .41): DeviceListService

**Files:**
- Create: `~/clawd/skcapstone-repos/skworld-app/lib/services/device_list_service.dart`
- Test: `~/clawd/skcapstone-repos/skworld-app/test/services/device_list_service_test.dart`

**Interfaces:**
- Consumes: the three endpoints from Task 8.
- Produces: `DeviceListService.list() -> Future<List<LinkedDevice>>`, `.unlink(String deviceFp) -> Future<void>`, `.unlinkOthers() -> Future<List<String>>`, and a `LinkedDevice` model with `deviceFp, label, labelSource, platform, enrolledAt, lastSeen, keyIds, isCurrent`.

**Run all Flutter commands on .41** (`ssh cbrd21@100.86.156.5`), using the non-snap `~/flutter`.

- [ ] **Step 1: Branch the app repo**

```bash
cd ~/clawd/skcapstone-repos/skworld-app
git checkout main && git pull
git checkout -b feat/linked-devices
```

- [ ] **Step 2: Read a sibling service first**

Run: `sed -n '1,80p' ~/clawd/skcapstone-repos/skworld-app/lib/services/guest_dm_contacts_service.dart`

Mirror its Dio setup, auth interceptor wiring, error handling, and test style exactly. Do not invent a new pattern.

- [ ] **Step 3: Write the failing test, modelled on that sibling's test file**

Cover: `list()` parses the envelope and the `is_current` flag; `unlink()` issues DELETE to the right path; `unlinkOthers()` returns the fingerprint list; a 400 from a self-unlink surfaces as a typed error rather than an unhandled exception.

- [ ] **Step 4: Run it and watch it fail**

Run: `cd ~/clawd/skcapstone-repos/skworld-app && ~/flutter/bin/flutter test test/services/device_list_service_test.dart`
Expected: FAIL, the service does not exist

- [ ] **Step 5: Implement the service, then re-run until green**

- [ ] **Step 6: Commit**

```bash
git add lib/services/device_list_service.dart test/services/device_list_service_test.dart
git commit -m "feat(devices): DeviceListService for the Linked Devices screen"
```

---

### Task 12 (app, on .41): Linked Devices screen and the signed label

**Files:**
- Create: `lib/features/profile/linked_devices_screen.dart`
- Modify: `lib/services/operator_session_service.dart:375-386` (the enroll call)
- Modify: `lib/features/profile/profile_screen.dart` (entry point)
- Test: `test/features/profile/linked_devices_screen_test.dart`

**Interfaces:**
- Consumes: `DeviceListService` (Task 11); the `label` field accepted by enroll (Task 6).
- Produces: the screen; enrollment that sends a signed label.

- [ ] **Step 1: Send the signed label at enrollment**

The signed payload must be canonical JSON with **sorted keys**, matching the server's `_canon`: `{"device_pubkey":...,"label":...,"nonce":...}`. Derive the label from real device info (`device_info_plus` if already a dependency, else platform plus host). Verify the dependency first:

Run: `grep -n "device_info_plus" ~/clawd/skcapstone-repos/skworld-app/pubspec.yaml`

If absent, use `Platform.operatingSystem` plus `Platform.localHostname` rather than adding a dependency for this.

- [ ] **Step 2: Write the failing widget test**

Cover: rows render label and relative last-seen; the current device shows a "This device" chip and has **no** Unlink control; tapping Unlink opens a confirm dialog and only calls the service on confirm; a non-operator `label_source` renders with the untrusted styling; the list refreshes after an unlink.

- [ ] **Step 3: Run it and watch it fail**

Run: `cd ~/clawd/skcapstone-repos/skworld-app && ~/flutter/bin/flutter test test/features/profile/linked_devices_screen_test.dart`

- [ ] **Step 4: Implement the screen, then re-run until green**

- [ ] **Step 5: Full app verification**

```bash
cd ~/clawd/skcapstone-repos/skworld-app
~/flutter/bin/flutter analyze --no-fatal-infos 2>&1 | grep -E "warning •|error •" || echo "no warnings or errors"
~/flutter/bin/flutter test
```

Expected: no `warning`/`error` lines, full suite green.

- [ ] **Step 6: Commit and PR**

```bash
git add -A
git commit -m "feat(devices): Linked Devices screen and signed device label at enrollment"
git push -u origin feat/linked-devices
gh pr create --base main --title "feat(devices): Linked Devices screen"
```

---

### Task 13: Live cutover on .158

Do this only after both PRs are merged.

- [ ] **Step 1: Show the operator what the reset will remove, before running it**

```bash
~/.skenv/bin/python3 -c "
import json, os, glob
p = os.path.expanduser('~/.skchat/state/operator_devices.json')
print('enrolled devices:', len(json.load(open(p)) if os.path.exists(p) else {}))
print('chef prekey slots:', len(glob.glob(os.path.expanduser('~/.skchat/pqc/peers/chef/*.json'))))
"
```

- [ ] **Step 2: Get explicit confirmation from Chef**

Every device has to re-link. This is his call to make, not yours. Do not run the reset without it.

- [ ] **Step 3: Run the reset and restart**

```bash
~/.skenv/bin/skchat devices reset --yes
systemctl --user restart skchat-daemon.service skchat-webui@lumina.service
sleep 8 && curl -s --max-time 5 http://localhost:9385/health
```

- [ ] **Step 4: Re-link this box and verify the round trip**

Link one device from the app, then confirm the registry row appears with a populated `key_ids` after its first prekey publish:

```bash
~/.skenv/bin/python3 -c "
import json, os
p = os.path.expanduser('~/.skchat/state/operator_device_registry.json')
print(json.dumps(json.load(open(p)), indent=2) if os.path.exists(p) else 'registry empty')
"
```

Expected: one row, with `label` from the client, `label_source: "client"`, and one entry in `key_ids`.

- [ ] **Step 5: Update memory**

Add a note to `~/.claude/projects/-home-cbrd21-clawd/memory/` recording that the clean cut happened, the date, and that any device not re-linked since is intentionally locked out. Link it from `MEMORY.md` and from `[[skchat-multidevice-dm-fanout]]`.

---

## Deferred to later phases (do not build now)

- **Phase 2 rename:** `PATCH /api/v1/operator/devices/{device_fp}` `{label}` setting `label_source: "operator"`, plus the inline rename affordance.
- **Phase 3 approval-to-link:** pending enrollments, slot quarantine, approve/deny endpoints and UI. Ships last because it changes the enrollment trust model.
- **Cross-node device sync:** out of scope per the spec; the registry is per-node like the rest of skchat state.
