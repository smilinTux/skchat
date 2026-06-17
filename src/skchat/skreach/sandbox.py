"""Exec sandbox — F1 §5.

Implements the F1 exec sandbox with the primary security gate:

    SKREACH_EXEC_ENABLED (env var, default "0")

When SKREACH_EXEC_ENABLED != "1", run() returns ExecDisabled without ever
calling subprocess.  All validation logic (cwd, denylist, env scrub) still
runs and is fully testable without actually spawning anything.

Controls implemented:
  §5.2  cwd validation: must be under allowed_cwd prefix; symlinks resolved.
  §5.3  env scrubbing: keys matching secret/token/password/key/credential stripped.
  §5.4  resource limits (RLIMIT_CPU, RLIMIT_NOFILE, RLIMIT_FSIZE, RLIMIT_AS,
         RLIMIT_NPROC; wall-clock watchdog via threading.Timer).
  §5.5  hardcoded denylist: shells, interpreters, raw network tools, etc.
        shell=False is ALWAYS used — never shell=True.
  §5.1  UID isolation: run as SKREACH_RUN_USER if set (not enforced in MVP —
        logged as a TODO; enforced by the systemd unit in production).

Spec: docs/superpowers/specs/2026-06-13-skreach-security.md §5
"""

from __future__ import annotations

import logging
import os
import re
import resource
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exec enable gate
# ---------------------------------------------------------------------------

_EXEC_ENABLED_VAR = "SKREACH_EXEC_ENABLED"


def _exec_enabled() -> bool:
    """Return True only when SKREACH_EXEC_ENABLED is explicitly "1"."""
    return os.environ.get(_EXEC_ENABLED_VAR, "0").strip() == "1"


# ---------------------------------------------------------------------------
# Hardcoded denylist (§5.5)
# Always denied regardless of role, allowlist, or node policy.
# ---------------------------------------------------------------------------

HARDCODED_DENYLIST: frozenset[str] = frozenset(
    [
        # Shells
        "bash",
        "sh",
        "zsh",
        "fish",
        "dash",
        "csh",
        "tcsh",
        "ksh",
        # Script interpreters
        "python",
        "python2",
        "python3",
        "pypy",
        "ruby",
        "perl",
        "lua",
        "node",
        "nodejs",
        "deno",
        # Raw network tools
        "nc",
        "ncat",
        "netcat",
        "socat",
        # Arbitrary HTTP fetch
        "curl",
        "wget",
        "fetch",
        # Raw I/O (used in shell injection chains)
        "tee",
        "dd",
        # Permission escalation
        "chmod",
        "chown",
        "chgrp",
        # Filesystem ops
        "mount",
        "umount",
    ]
)

# ---------------------------------------------------------------------------
# Env scrub pattern (§5.3)
# Any key name matching this pattern (case-insensitive) is stripped.
# ---------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(r"(?i).*(?:key|secret|token|password|credential).*")

# Minimum safe env injected into every child process (§5.3)
_SAFE_ENV_BASE = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ExecDisabled:
    """Returned by run() when SKREACH_EXEC_ENABLED is not "1".

    Validation (cwd, denylist, env scrub) has already been performed;
    the result carries the sanitised argv and any stripped keys for audit.
    """

    argv: list[str]
    cwd: str
    scrubbed_keys: list[str]  # env key names that were stripped
    message: str = "exec disabled (SKREACH_EXEC_ENABLED != 1)"


@dataclass
class ExecResult:
    """Returned by run() when exec is enabled and the command completes."""

    argv: list[str]
    cwd: str
    scrubbed_keys: list[str]
    exit_code: Optional[int]
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    outcome: str = "executed"  # "executed" | "timeout" | "error"
    error: str = ""


@dataclass
class ValidationError(Exception):
    """Raised by run() when a pre-exec validation check fails."""

    message: str
    outcome: str  # "allowlist_denied" | "error"


# ---------------------------------------------------------------------------
# Sandbox configuration (per-node policy; §3.4, §5.4)
# ---------------------------------------------------------------------------


@dataclass
class SandboxConfig:
    """Per-node sandbox policy loaded from skreach-node.yaml.

    Attributes:
        allowed_cwd:        Working-directory prefix whitelist (§5.2).
        command_denylist:   Additional per-node denied binary names (§3.4).
                            The HARDCODED_DENYLIST is ALWAYS applied on top.
        wall_clock_timeout: Max wall-clock seconds before SIGKILL (§5.4).
        cpu_limit_s:        RLIMIT_CPU (seconds; §5.4).
        max_open_files:     RLIMIT_NOFILE (§5.4).
        max_file_size_mb:   RLIMIT_FSIZE in MB (§5.4).
        max_vm_mb:          RLIMIT_AS in MB (§5.4).
        max_procs:          RLIMIT_NPROC (§5.4).
        node_fqid:          This node's FQID (injected into child env).
        skreach_home:       Skreach home dir (injected as HOME, §5.3).
    """

    allowed_cwd: list[str] = field(default_factory=lambda: ["/opt/skworld"])
    command_denylist: list[str] = field(default_factory=list)
    wall_clock_timeout: float = 600.0
    cpu_limit_s: int = 300
    max_open_files: int = 256
    max_file_size_mb: int = 512
    max_vm_mb: int = 2048
    max_procs: int = 64
    node_fqid: str = ""
    skreach_home: str = str(Path.home())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_cwd(cwd: str, allowed_prefixes: list[str]) -> str:
    """Validate and resolve *cwd* against *allowed_prefixes*.

    Returns the canonicalised real path if valid.

    Raises:
        ValidationError: If cwd does not exist or is outside all allowed prefixes.
    """
    p = Path(cwd)
    if not p.exists():
        raise ValidationError(message=f"cwd '{cwd}' does not exist", outcome="error")

    try:
        real = p.resolve(strict=True)  # follows symlinks; raises if broken
    except (OSError, RuntimeError) as exc:
        raise ValidationError(
            message=f"cwd '{cwd}' could not be resolved: {exc}", outcome="error"
        ) from exc

    real_str = str(real)
    for prefix in allowed_prefixes:
        canonical_prefix = str(Path(prefix).resolve())
        if real_str == canonical_prefix or real_str.startswith(canonical_prefix + "/"):
            return real_str

    raise ValidationError(
        message=f"cwd '{cwd}' (resolved: '{real_str}') is outside allowed_cwd prefixes",
        outcome="allowlist_denied",
    )


def _scrub_env(
    extra_env: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Strip secret-named keys from *extra_env*.

    Returns:
        (clean_env, scrubbed_key_names): the scrubbed dict and a list of the
        key names that were removed (names only, never values).
    """
    clean: dict[str, str] = {}
    scrubbed: list[str] = []
    for k, v in extra_env.items():
        if _SECRET_KEY_RE.match(k):
            scrubbed.append(k)
        else:
            clean[k] = v
    return clean, scrubbed


def _build_child_env(
    extra_env: dict[str, str],
    node_fqid: str,
    cmd_id: str,
    skreach_home: str,
) -> tuple[dict[str, str], list[str]]:
    """Construct the child process environment from scratch (§5.3).

    Starts from the minimal safe base, merges extra_env (after scrubbing),
    injects the standard skreach vars.

    Returns:
        (child_env, scrubbed_key_names)
    """
    clean_extra, scrubbed = _scrub_env(extra_env)
    env: dict[str, str] = {
        **_SAFE_ENV_BASE,
        "HOME": skreach_home,
        "SKAGENT": node_fqid,
        "SKREACH_CMD_ID": cmd_id,
    }
    env.update(clean_extra)
    return env, scrubbed


def _check_denylist(binary: str, extra_denylist: list[str]) -> None:
    """Raise ValidationError if *binary* is in the hardcoded or per-node denylist.

    *binary* should be the basename (no path), lowercased before calling.
    """
    name = Path(binary).name.lower()
    full_deny = HARDCODED_DENYLIST | frozenset(b.lower() for b in extra_denylist)
    if name in full_deny:
        raise ValidationError(
            message=f"command '{binary}' is in the denylist",
            outcome="allowlist_denied",
        )


def _set_resource_limits(cfg: SandboxConfig) -> None:
    """Apply hard resource limits for the child process (§5.4).

    Called via preexec_fn — runs in the forked child before exec.
    """
    mb = 1024 * 1024

    def _try_set(res: int, soft: int, hard: int) -> None:
        try:
            resource.setrlimit(res, (soft, hard))
        except (ValueError, resource.error):
            pass  # best-effort; platform may not support all limits

    _try_set(resource.RLIMIT_CPU, cfg.cpu_limit_s, cfg.cpu_limit_s)
    _try_set(resource.RLIMIT_NOFILE, cfg.max_open_files, cfg.max_open_files)
    _try_set(resource.RLIMIT_FSIZE, cfg.max_file_size_mb * mb, cfg.max_file_size_mb * mb)
    # RLIMIT_AS may not be available on all platforms
    if hasattr(resource, "RLIMIT_AS"):
        _try_set(resource.RLIMIT_AS, cfg.max_vm_mb * mb, cfg.max_vm_mb * mb)
    if hasattr(resource, "RLIMIT_NPROC"):
        _try_set(resource.RLIMIT_NPROC, cfg.max_procs, cfg.max_procs)


# ---------------------------------------------------------------------------
# Public run() function
# ---------------------------------------------------------------------------


def run(
    argv: list[str],
    *,
    cwd: str,
    extra_env: dict[str, str],
    cmd_id: str = "",
    config: Optional[SandboxConfig] = None,
) -> "ExecDisabled | ExecResult":
    """Execute *argv* in the F1 sandbox.

    Validation (cwd, denylist, env scrub) ALWAYS runs, even when exec is
    disabled.  This makes all validation logic unit-testable without exec.

    When SKREACH_EXEC_ENABLED != "1", returns ExecDisabled immediately after
    validation — no subprocess is spawned.

    shell=False is UNCONDITIONALLY enforced.  Any future call that passes
    shell=True to subprocess would be a bug — the test suite asserts this.

    Args:
        argv:       The command + arguments as a list (no shell interpolation).
        cwd:        Working directory (validated against config.allowed_cwd).
        extra_env:  Additional env vars (scrubbed before passing to child).
        cmd_id:     The command envelope id (injected as SKREACH_CMD_ID).
        config:     Per-node SandboxConfig; uses a default if None.

    Returns:
        ExecDisabled if exec is gated off; ExecResult if exec runs.

    Raises:
        ValidationError: On cwd violation, denylist match, or empty argv.
    """
    if config is None:
        config = SandboxConfig()

    if not argv:
        raise ValidationError(message="argv is empty", outcome="error")

    # --- §5.5 Denylist check (hardcoded + per-node) ---
    binary = argv[0]
    _check_denylist(binary, config.command_denylist)

    # --- §5.2 CWD validation + symlink resolution ---
    validated_cwd = _resolve_cwd(cwd, config.allowed_cwd)

    # --- §5.3 Env scrub + build child env ---
    child_env, scrubbed_keys = _build_child_env(
        extra_env, config.node_fqid, cmd_id, config.skreach_home
    )

    # --- EXEC GATE ---
    if not _exec_enabled():
        logger.debug(
            "skreachd exec DISABLED (set %s=1 to enable); cmd_id=%s argv=%r",
            _EXEC_ENABLED_VAR,
            cmd_id,
            argv,
        )
        return ExecDisabled(argv=argv, cwd=validated_cwd, scrubbed_keys=scrubbed_keys)

    # --- Actual subprocess execution (only when explicitly enabled) ---
    # shell=False is NEVER changed. Do not pass shell=True anywhere in skreachd.
    proc: Optional[subprocess.Popen] = None
    timed_out = False

    try:
        proc = subprocess.Popen(
            args=argv,  # list[str]; NO shell interpolation
            cwd=validated_cwd,
            env=child_env,
            shell=False,  # NEVER True — §5.5 invariant
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=lambda: _set_resource_limits(config),  # type: ignore[arg-type]
        )
    except (OSError, ValueError) as exc:
        logger.error("skreachd: subprocess spawn failed: %s", exc)
        return ExecResult(
            argv=argv,
            cwd=validated_cwd,
            scrubbed_keys=scrubbed_keys,
            exit_code=None,
            stdout=b"",
            stderr=str(exc).encode(),
            outcome="error",
            error=str(exc),
        )

    # Wall-clock watchdog (§5.4)
    def _kill_on_timeout() -> None:
        nonlocal timed_out
        if proc and proc.poll() is None:
            timed_out = True
            try:
                proc.kill()
            except OSError:
                pass

    timer = threading.Timer(config.wall_clock_timeout, _kill_on_timeout)
    timer.start()
    try:
        stdout_bytes, stderr_bytes = proc.communicate()
    finally:
        timer.cancel()

    exit_code = proc.returncode
    outcome = "timeout" if timed_out else "executed"

    return ExecResult(
        argv=argv,
        cwd=validated_cwd,
        scrubbed_keys=scrubbed_keys,
        exit_code=exit_code,
        stdout=stdout_bytes,
        stderr=stderr_bytes,
        timed_out=timed_out,
        outcome=outcome,
    )
