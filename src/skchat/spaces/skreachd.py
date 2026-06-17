"""skreachd — sandboxed command-execution backend for the Spaces *term* lane.

The Spaces terminal lane broadcasts run requests as::

    {"lane": "term", "action": "run", "cmd": <str>, "id": <id>, "from": <identity>}

and consumers expect a stream of reply envelopes::

    {"lane": "term", "action": "output", "id": <id>, "chunk": <str>, "stream": "stdout"|"stderr"}
    {"lane": "term", "action": "exit",   "id": <id>, "code": <int>}

This module is the (previously missing) backend that turns a ``run`` request
into those reply envelopes — SAFELY. It is command execution, so security is
the primary concern, not an afterthought.

SECURITY MODEL (defaults are the safe path; hard to disable)
------------------------------------------------------------
* **opt-in.** Exec is OFF unless ``SKREACHD_ENABLED`` (or
  ``SandboxPolicy.enabled``) is explicitly truthy. When off, ``run`` emits a
  single ``exec_disabled`` event and NEVER touches a runner.
* **argv only, never a shell.** The command string is parsed with
  ``shlex.split`` into an argv list. The real runner uses ``subprocess`` with
  ``shell=False``. Shell metacharacters (``;`` ``|`` ``&`` ``$(...)`` ``>``)
  are therefore inert: ``ls; rm -rf /`` parses to the argv
  ``["ls", ";", "rm", "-rf", "/"]`` — ``rm`` is never executed and the literal
  ``;`` token simply gets passed to ``ls`` (which errors). No shell ever sees
  the string.
* **executable allowlist.** The first argv token (and, for multi-word entries
  like ``git status``, the leading tokens) must match a configurable allowlist.
  The default is a small read-only set. Anything else → ``denied``.
* **RBAC.** Only an authorized ``from`` identity may run. The operator set is
  configurable (``SKREACHD_OPERATORS`` env, comma/space separated). An empty
  operator set denies everyone (fail-closed). Unauthorized → ``denied``.
* **scoped cwd + timeout + output cap.** Commands run in a sandbox directory
  (default ``~/.skchat/skreachd-sandbox``), with a wall-clock timeout (default
  15s) and a captured-output size cap. The child env is built from a minimal
  safe base — the parent process environment (and its secrets) is NOT inherited.

The executor is runner-injectable so tests never spawn real processes.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

LANE = "term"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Default executable allowlist — a conservative read-only set. Multi-token
#: entries (e.g. "git status") match against the leading argv tokens, so other
#: git subcommands (push, commit, ...) are NOT permitted.
DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "ls",
    "pwd",
    "echo",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "git status",
    "git log",
    "df",
    "uptime",
    "whoami",
    "date",
)

DEFAULT_TIMEOUT_S: float = 15.0
DEFAULT_MAX_OUTPUT_BYTES: int = 256 * 1024  # 256 KiB cap per stream

_ENABLED_ENV = "SKREACHD_ENABLED"
_OPERATORS_ENV = "SKREACHD_OPERATORS"
_SANDBOX_ENV = "SKREACHD_SANDBOX_DIR"
_TIMEOUT_ENV = "SKREACHD_TIMEOUT_S"

# Minimal env handed to children — the parent env (with its secrets) is never
# inherited. PATH is restricted to standard system locations.
_SAFE_ENV_BASE = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C",
}


def _env_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _default_sandbox_dir() -> Path:
    raw = os.getenv(_SANDBOX_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".skchat" / "skreachd-sandbox"


# ---------------------------------------------------------------------------
# Runner seam
# ---------------------------------------------------------------------------


@dataclass
class RunnerResult:
    """Result of executing an argv list via a runner."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


#: A runner takes (argv, cwd, timeout, max_bytes) and returns a RunnerResult.
#: Tests inject a fake runner so no real process is ever spawned.
Runner = Callable[[list[str], str, float, int], RunnerResult]


def subprocess_runner(argv: list[str], cwd: str, timeout: float, max_bytes: int) -> RunnerResult:
    """The real runner — argv-list subprocess, ``shell=False``, scrubbed env.

    Output is captured and truncated to *max_bytes* per stream. A wall-clock
    timeout is enforced; on timeout the process is killed and ``timed_out`` set.
    """
    try:
        proc = subprocess.run(  # noqa: S603  (shell=False; argv list; intentional)
            argv,
            cwd=cwd,
            env=dict(_SAFE_ENV_BASE),
            shell=False,  # NEVER True — core invariant of skreachd
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace")[:max_bytes]
        err = (exc.stderr or b"").decode("utf-8", "replace")[:max_bytes]
        if not err:
            err = f"command timed out after {timeout}s"
        return RunnerResult(exit_code=124, stdout=out, stderr=err, timed_out=True)
    except (OSError, ValueError) as exc:
        return RunnerResult(exit_code=127, stdout="", stderr=str(exc))

    stdout = proc.stdout.decode("utf-8", "replace")[:max_bytes]
    stderr = proc.stderr.decode("utf-8", "replace")[:max_bytes]
    return RunnerResult(exit_code=proc.returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass
class SandboxPolicy:
    """Security policy for the executor. Defaults are the safe path.

    Attributes:
        enabled:          Exec is OFF unless this is True (defaults to the
                          ``SKREACHD_ENABLED`` env var). Opt-in by design.
        operators:        Allowed ``from`` identities (RBAC). Defaults to the
                          ``SKREACHD_OPERATORS`` env var (comma/space split).
                          Empty → deny everyone (fail-closed).
        allowlist:        Allowed executables / leading-token prefixes.
        cwd:              Scoped working directory (created if missing).
        timeout_s:        Wall-clock timeout per command.
        max_output_bytes: Per-stream output cap.
    """

    enabled: bool = field(default_factory=lambda: _env_truthy(os.getenv(_ENABLED_ENV)))
    operators: frozenset[str] = field(default_factory=lambda: _operators_from_env())
    allowlist: tuple[str, ...] = DEFAULT_ALLOWLIST
    cwd: Path = field(default_factory=_default_sandbox_dir)
    timeout_s: float = field(
        default_factory=lambda: float(os.getenv(_TIMEOUT_ENV, DEFAULT_TIMEOUT_S))
    )
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES


def _operators_from_env() -> frozenset[str]:
    raw = os.getenv(_OPERATORS_ENV, "")
    parts = [p.strip() for p in raw.replace(",", " ").split()]
    return frozenset(p for p in parts if p)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class SkreachExecutor:
    """Turns a *term*-lane ``run`` request into reply envelopes — safely.

    Usage::

        ex = SkreachExecutor(SandboxPolicy(enabled=True, operators={"chef@..."}))
        events = ex.run("ls -la", identity="chef@...", cmd_id="abc")

    ``run`` returns a list of term-lane event dicts:
      * a single ``exec_disabled`` event when exec is gated off,
      * a single ``denied`` event when RBAC or the allowlist rejects the command,
      * otherwise one or more ``output`` events followed by one ``exit`` event.
    """

    def __init__(
        self,
        policy: Optional[SandboxPolicy] = None,
        *,
        runner: Optional[Runner] = None,
    ) -> None:
        self.policy = policy or SandboxPolicy()
        self._runner = runner or subprocess_runner

    # -- event builders -----------------------------------------------------

    @staticmethod
    def _ev(action: str, cmd_id, **extra) -> dict:
        ev = {"lane": LANE, "action": action, "id": cmd_id}
        ev.update(extra)
        return ev

    # -- allowlist ----------------------------------------------------------

    def _allowlisted(self, argv: list[str]) -> bool:
        """True if *argv* matches an allowlist entry by leading tokens."""
        for entry in self.policy.allowlist:
            tokens = entry.split()
            if not tokens:
                continue
            if argv[: len(tokens)] == tokens:
                return True
        return False

    # -- main API -----------------------------------------------------------

    def run(self, cmd: str, *, identity: str, cmd_id="") -> list[dict]:
        """Validate and (if permitted) execute *cmd*; return term-lane events."""
        # 1. Opt-in gate — never touch the runner when disabled.
        if not self.policy.enabled:
            return [
                self._ev(
                    "exec_disabled",
                    cmd_id,
                    reason=f"exec disabled (set {_ENABLED_ENV}=1 to enable)",
                )
            ]

        # 2. RBAC — only an authorized identity may run (fail-closed).
        if not identity or identity not in self.policy.operators:
            return [
                self._ev(
                    "denied",
                    cmd_id,
                    reason="identity not authorized to run commands",
                    identity=identity,
                )
            ]

        # 3. Parse into argv — NEVER a shell. shlex handles quoting/metachars.
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            return [self._ev("denied", cmd_id, reason=f"could not parse command: {exc}")]
        if not argv:
            return [self._ev("denied", cmd_id, reason="empty command")]

        # 4. Allowlist the executable (leading tokens).
        if not self._allowlisted(argv):
            return [
                self._ev(
                    "denied",
                    cmd_id,
                    reason=f"command not allowlisted: {argv[0]!r}",
                    argv=argv,
                )
            ]

        # 5. Scoped cwd — create the sandbox if needed.
        cwd = self.policy.cwd
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return [self._ev("denied", cmd_id, reason=f"sandbox cwd unavailable: {exc}")]

        # 6. Execute via the (injectable) runner.
        result = self._runner(argv, str(cwd), self.policy.timeout_s, self.policy.max_output_bytes)

        events: list[dict] = []
        if result.stdout:
            events.append(self._ev("output", cmd_id, chunk=result.stdout, stream="stdout"))
        if result.stderr:
            events.append(self._ev("output", cmd_id, chunk=result.stderr, stream="stderr"))
        events.append(self._ev("exit", cmd_id, code=result.exit_code))
        return events
