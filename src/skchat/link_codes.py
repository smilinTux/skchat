"""Short-lived, single-use codes for linking a device.

The operator token is a long-lived shared secret living in plaintext env files,
presented by other services, with no generator and no rotation path. It is also
what the operator currently types into a phone to link it. That is the wrong
shape for a bootstrap credential: a secret you paste into clients wants to be
short-lived and single-use, and a long-lived service credential should ideally
never leave the box.

A link code is that bootstrap credential. Minted on the box (shell access
already implies total control), it expires in minutes and burns on first use.

Two properties carry the security weight:

* **Enrollment only.** ``guest._require_operator`` also guards guest invites,
  prekey signing and call routes. A code that opened those would be a strictly
  worse operator token rather than a better one, so it is accepted ONLY on the
  route where a device links itself.
* **Hash at rest.** Only ``sha256(code)`` is stored, so a readable state file
  does not hand anyone a working code. The plaintext lives in the operator's
  terminal (and the QR they scan) and nowhere else.

It is presented in the SAME header as the operator token on purpose: the app
already has a paste field wired to that header, so a short-lived code drops into
the existing flow with no client change.

Fails closed everywhere: no file, corrupt file, expired, already burned, or an
unrecognised code all mean "refuse".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

logger = logging.getLogger("skchat.link_codes")

#: Override the store location (tests point this at tmp_path).
STORE_PATH_ENV = "SKCHAT_LINK_CODES"
_DEFAULT_STORE = "~/.skchat/state/link_codes.json"

#: How long a freshly minted code stays usable. Long enough to walk to another
#: device and type or scan it, short enough that a code left on a screen is not
#: a standing key to the node.
DEFAULT_TTL_SECONDS = 600

#: Groups of 4 from an unambiguous alphabet. Deliberately NOT base64: this gets
#: read off a screen and typed on a phone, so 0/O and 1/l/I are excluded to stop
#: transcription errors being mistaken for a rejected code.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_GROUPS = 4
_GROUP_LEN = 4

_lock = threading.Lock()


def store_path() -> Path:
    raw = os.getenv(STORE_PATH_ENV, "").strip() or _DEFAULT_STORE
    return Path(raw).expanduser()


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _load() -> list[dict]:
    """Stored entries. A missing or corrupt store reads as empty (fail closed)."""
    path = store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text() or "[]")
    except (ValueError, OSError):
        logger.warning("link code store unreadable, treating as empty: %s", path)
        return []
    return data if isinstance(data, list) else []


def _save(entries: list[dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(entries, indent=2))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort
        pass


def mint(*, ttl: int = DEFAULT_TTL_SECONDS, now: float | None = None) -> str:
    """Create a code, store only its hash, and return the plaintext ONCE.

    The returned string is the only copy that will ever exist outside the
    operator's screen: nothing recoverable is written to disk.
    """
    now = time.time() if now is None else now
    code = "-".join(
        "".join(secrets.choice(_ALPHABET) for _ in range(_GROUP_LEN)) for _ in range(_GROUPS)
    )
    with _lock:
        entries = [e for e in _load() if (e.get("expires_at") or 0) > now]
        entries.append({"hash": _hash(code), "expires_at": now + float(ttl)})
        _save(entries)
    logger.info("minted a device link code valid for %ss", ttl)
    return code


def verify(code: str, *, now: float | None = None) -> bool:
    """True if *code* is live, and BURN it. False for anything else.

    Burning happens in the same locked section as the check, so two devices
    racing the same code cannot both be admitted.
    """
    if not code or not code.strip():
        return False
    now = time.time() if now is None else now
    wanted = _hash(code)
    with _lock:
        entries = _load()
        kept: list[dict] = []
        found = False
        for e in entries:
            alive = (e.get("expires_at") or 0) > now
            if not alive:
                continue  # prune while we are here
            if not found and secrets.compare_digest(str(e.get("hash") or ""), wanted):
                found = True  # burn: do not carry it forward
                continue
            kept.append(e)
        if len(kept) != len(entries):
            _save(kept)
    if found:
        logger.info("a device link code was used")
    return found


def revoke_all() -> int:
    """Drop every outstanding code. Returns how many were dropped."""
    with _lock:
        entries = _load()
        _save([])
        return len(entries)


def outstanding(*, now: float | None = None) -> int:
    """How many codes are currently live (for operator-facing output)."""
    now = time.time() if now is None else now
    return len([e for e in _load() if (e.get("expires_at") or 0) > now])
