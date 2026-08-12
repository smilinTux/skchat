"""Generate and rotate the operator token.

The token had no generator and no rotation path: it was created by hand once and
never changed. If it leaked, the only recourse was hand-editing env files and
guessing which services to restart.

Rotation is easy to get dangerously wrong, so the risky part lives here as pure,
tested functions and the systemd side stays thin on top:

* The token is **not just a login secret**. It is presented BY services (the call
  answerer holds it), so a rotation is only complete once every consumer has been
  restarted with the new value.
* It **shares its env file** with everything else those units need
  (``SKCHAT_DATAPLANE_AUTH``, ``SKCHAT_AUTHZ_PDP``, and more). Rewriting the file
  has to leave every other line exactly as it was.
* It is **per agent**. ``webui-lumina.env`` and ``webui-opus.env`` carry
  different tokens on the live node, so rotating one must not disturb the other.
* The file is **owner-only**, and must stay that way. A rotation that leaves it
  world-readable would quietly undo the thing it was run to protect.

Deliberately NOT auto-rotating on a timer: coordinated restarts of several units
is not something to do unattended, and a failed restart at 3am breaks the plane
in a way nobody is watching for. See ``skchat devices link`` for the piece that
removes the day-to-day reason for the token to leave the box at all.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
from pathlib import Path

logger = logging.getLogger("skchat.operator_token")

#: The env var the gate reads.
TOKEN_VAR = "SKCHAT_GUEST_OPERATOR_TOKEN"

#: Where the per-agent env files live (override for tests).
ENV_DIR_VAR = "SKCHAT_ENV_DIR"
_DEFAULT_ENV_DIR = "~/.config/skchat"

#: Only real unit env files, never the .bak-* snapshots sitting beside them.
_ENV_GLOB = "webui-*.env"


def env_dir() -> Path:
    raw = os.getenv(ENV_DIR_VAR, "").strip() or _DEFAULT_ENV_DIR
    return Path(raw).expanduser()


def generate() -> str:
    """A fresh token. 32 bytes of urandom, url-safe, same shape as the current one."""
    return secrets.token_urlsafe(32)


def fingerprint(token: str) -> str:
    """A short, stable identifier for a token that does not reveal it.

    Lets ``show`` and ``rotate`` confirm which token a file or a running process
    carries, and prove a restart actually picked the new one up, without ever
    printing the secret into a terminal or a log.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def env_files(*, agent: str | None = None) -> list[Path]:
    """Env files that actually carry a token, optionally for one agent only."""
    out: list[Path] = []
    base = env_dir()
    if not base.is_dir():
        return out
    for path in sorted(base.glob(_ENV_GLOB)):
        if agent and path.stem != f"webui-{agent}":
            continue
        if read_token(path) is not None:
            out.append(path)
    return out


def agent_of(path: Path) -> str:
    """``webui-lumina.env`` -> ``lumina``."""
    return path.stem.removeprefix("webui-")


def read_token(path: Path) -> str | None:
    """The token in *path*, or None if it carries none."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{TOKEN_VAR}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def write_token(path: Path, token: str, *, backup: bool = False) -> Path | None:
    """Replace the token in *path*, leaving every other line untouched.

    Atomic (tmp in the same directory, then ``os.replace``) and owner-only, so a
    crash mid-write cannot leave a torn env file and a rotation cannot widen the
    permissions on a secrets file.

    Raises:
        ValueError: if *path* has no token line. Refusing beats appending one:
            a file without the var is not a file this rotation understands, and
            half-configuring a unit is worse than declining.

    Returns the backup path when ``backup`` is set, else None.
    """
    lines = path.read_text().splitlines(keepends=True)
    if not any(line.startswith(f"{TOKEN_VAR}=") for line in lines):
        raise ValueError(f"{path} has no {TOKEN_VAR} line; refusing to invent one")

    made: Path | None = None
    if backup:
        made = path.with_suffix(path.suffix + f".bak-{fingerprint(read_token(path) or '')}")
        shutil.copy2(path, made)
        os.chmod(made, 0o600)

    out = [
        f"{TOKEN_VAR}={token}\n" if line.startswith(f"{TOKEN_VAR}=") else line for line in lines
    ]
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        tmp.write_text("".join(out))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():  # pragma: no cover - only on a failed replace
            tmp.unlink(missing_ok=True)
    logger.info("rotated %s in %s", TOKEN_VAR, path.name)
    return made


def consumers(agent: str) -> list[str]:
    """Systemd units that must be restarted for a rotation to take effect.

    The web UI serves the gate; the call answerer PRESENTS the token, so it holds
    a stale copy until restarted and would start failing its own calls.
    """
    return [f"skchat-webui@{agent}.service", f"skchat-call-answerer@{agent}.service"]


def running_token_fingerprint(unit: str) -> str | None:
    """The fingerprint of the token a RUNNING unit actually has in its env.

    This is what proves a restart picked the new value up. Reading the file back
    only proves the file changed, which is the easy half.
    """
    try:
        import subprocess

        pid = subprocess.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", unit],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if not pid or pid == "0":
            return None
        raw = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="ignore")
        for entry in raw.split("\0"):
            if entry.startswith(f"{TOKEN_VAR}="):
                return fingerprint(entry.split("=", 1)[1])
    except Exception:
        return None
    return None
