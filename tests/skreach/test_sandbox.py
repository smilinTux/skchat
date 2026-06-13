"""Tests for skchat.skreach.sandbox — F1 §5.

Covers acceptance criteria:
  SAND-1: shell=True is never used (static check + runtime assertion)
  SAND-3: cwd outside allowed_cwd → ValidationError (allowlist_denied)
  SAND-4: cwd is a symlink pointing outside allowed_cwd → rejected after resolution
  SAND-5: denylist binary rejected even if it would otherwise be allowed
  SAND-7: secret-named env keys stripped; scrubbed list logged; child never sees them
  + EXEC GATED: with exec disabled, no subprocess is ever spawned (ExecDisabled returned)
  + cwd that does not exist → ValidationError (error)
  + empty argv → ValidationError (error)
  + allowed cwd passes
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from skchat.skreach.sandbox import (
    HARDCODED_DENYLIST,
    ExecDisabled,
    ExecResult,
    SandboxConfig,
    ValidationError,
    _scrub_env,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, extra_deny: list[str] | None = None) -> SandboxConfig:
    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    return SandboxConfig(
        allowed_cwd=[str(allowed)],
        command_denylist=extra_deny or [],
        wall_clock_timeout=5.0,
        node_fqid="noroc2027@chef.skworld.io",
        skreach_home=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# EXEC GATED — primary security property
# ---------------------------------------------------------------------------


def test_exec_disabled_returns_ExecDisabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With SKREACH_EXEC_ENABLED unset (default), run() returns ExecDisabled."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    cwd = str(tmp_path / "allowed")

    result = run(["skcapstone", "status"], cwd=cwd, extra_env={}, config=cfg)
    assert isinstance(result, ExecDisabled), f"expected ExecDisabled, got {type(result)}"


def test_exec_disabled_no_subprocess_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With exec disabled, subprocess.Popen is NEVER called."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    cwd = str(tmp_path / "allowed")

    with mock.patch("subprocess.Popen") as mock_popen:
        run(["skcapstone", "status"], cwd=cwd, extra_env={}, config=cfg)
        mock_popen.assert_not_called()


def test_exec_disabled_explicitly_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SKREACH_EXEC_ENABLED=0 explicitly also gates exec."""
    monkeypatch.setenv("SKREACH_EXEC_ENABLED", "0")
    cfg = _make_config(tmp_path)
    cwd = str(tmp_path / "allowed")

    result = run(["skcapstone", "status"], cwd=cwd, extra_env={}, config=cfg)
    assert isinstance(result, ExecDisabled)


def test_exec_disabled_carries_scrubbed_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ExecDisabled result includes the names of scrubbed secret env keys."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    cwd = str(tmp_path / "allowed")

    env = {"MY_SECRET_KEY": "hunter2", "SAFE_VAR": "ok", "API_TOKEN": "tok"}
    result = run(["skcapstone", "status"], cwd=cwd, extra_env=env, config=cfg)

    assert isinstance(result, ExecDisabled)
    assert "MY_SECRET_KEY" in result.scrubbed_keys
    assert "API_TOKEN" in result.scrubbed_keys
    assert "SAFE_VAR" not in result.scrubbed_keys


# ---------------------------------------------------------------------------
# SAND-1: shell=False invariant
# ---------------------------------------------------------------------------


def test_shell_true_never_used_in_source() -> None:
    """SAND-1: static/AST check — subprocess.Popen is never called with shell=True.

    We use the AST rather than a raw string search so that occurrences in
    docstrings or comments (e.g. '# NEVER shell=True') do not trigger a false
    positive.  We look for ast.keyword nodes whose arg=='shell' and whose value
    is a True constant — that would be an actual shell=True keyword argument.
    """
    import ast
    import inspect
    import skchat.skreach.sandbox as _sandbox_module

    src = inspect.getsource(_sandbox_module)
    tree = ast.parse(src)

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (
                    kw.arg == "shell"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    violations.append(
                        (getattr(node, "lineno", 0), ast.unparse(node))
                    )

    assert not violations, (
        "SAND-1 VIOLATION: shell=True keyword argument found in sandbox.py!\n"
        + "\n".join(f"  line {ln}: {snip}" for ln, snip in violations)
    )


# ---------------------------------------------------------------------------
# SAND-3: cwd outside allowed_cwd → rejected
# ---------------------------------------------------------------------------


def test_cwd_outside_allowed_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAND-3: cwd outside all allowed_cwd prefixes → ValidationError."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        run(["skcapstone", "status"], cwd=str(outside), extra_env={}, config=cfg)
    assert exc_info.value.outcome in ("allowlist_denied", "error")


def test_cwd_traversal_attempt_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAND-3: cwd using .. traversal that lands outside allowed prefix → rejected."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    # allowed/../../ would resolve to tmp_path.parent — outside allowed
    # But Path.resolve() will canonicalise it before the prefix check
    allowed = tmp_path / "allowed"
    traversal = str(allowed / ".." / "..")  # resolves to tmp_path.parent
    with pytest.raises(ValidationError):
        run(["skcapstone", "status"], cwd=traversal, extra_env={}, config=cfg)


# ---------------------------------------------------------------------------
# SAND-4: symlink pointing outside allowed_cwd → rejected after resolution
# ---------------------------------------------------------------------------


def test_symlink_cwd_outside_allowed_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAND-4: symlink cwd → real path checked after resolution."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)

    # Create a directory outside allowed/ and a symlink inside allowed/ → outside
    outside = tmp_path / "secret"
    outside.mkdir()
    allowed = tmp_path / "allowed"
    link = allowed / "escape_link"
    link.symlink_to(outside)

    with pytest.raises(ValidationError) as exc_info:
        run(["skcapstone", "status"], cwd=str(link), extra_env={}, config=cfg)
    assert exc_info.value.outcome in ("allowlist_denied", "error")


def test_symlink_inside_allowed_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink that resolves to a path INSIDE allowed_cwd is accepted."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    allowed = tmp_path / "allowed"
    inner = allowed / "inner"
    inner.mkdir(parents=True)
    link = allowed / "link_to_inner"
    link.symlink_to(inner)

    cfg = SandboxConfig(
        allowed_cwd=[str(allowed)],
        command_denylist=[],
        wall_clock_timeout=5.0,
        node_fqid="node",
        skreach_home=str(tmp_path),
    )

    result = run(["skcapstone", "status"], cwd=str(link), extra_env={}, config=cfg)
    assert isinstance(result, ExecDisabled)  # exec disabled, but validation passed


# ---------------------------------------------------------------------------
# SAND-5: denylist binary rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "binary",
    ["bash", "sh", "python", "python3", "nc", "curl", "wget", "perl", "ruby"],
)
def test_hardcoded_denylist_binary_rejected(
    binary: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAND-5: every hardcoded-denylist binary is rejected even with valid cwd."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    cwd = str(tmp_path / "allowed")

    with pytest.raises(ValidationError) as exc_info:
        run([binary, "-c", "id"], cwd=cwd, extra_env={}, config=cfg)
    assert exc_info.value.outcome == "allowlist_denied"


def test_per_node_denylist_binary_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAND-5: a binary in the per-node denylist is also rejected."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path, extra_deny=["my_custom_dangerous_tool"])
    cwd = str(tmp_path / "allowed")

    with pytest.raises(ValidationError) as exc_info:
        run(["my_custom_dangerous_tool", "--help"], cwd=cwd, extra_env={}, config=cfg)
    assert exc_info.value.outcome == "allowlist_denied"


# ---------------------------------------------------------------------------
# SAND-7: Secret env keys are stripped
# ---------------------------------------------------------------------------


def test_secret_env_keys_stripped() -> None:
    """SAND-7: keys matching the secret pattern are stripped and their names recorded."""
    dirty = {
        "MY_SECRET": "s3cr3t",
        "GITHUB_TOKEN": "ghp_xxx",
        "DB_PASSWORD": "hunter2",
        "ANTHROPIC_API_KEY": "sk-...",
        "SAFE_VAR": "visible",
        "DEBUG": "1",
    }
    clean, scrubbed = _scrub_env(dirty)

    # Scrubbed key names are reported
    assert "MY_SECRET" in scrubbed
    assert "GITHUB_TOKEN" in scrubbed
    assert "DB_PASSWORD" in scrubbed
    assert "ANTHROPIC_API_KEY" in scrubbed

    # Safe vars pass through
    assert "SAFE_VAR" in clean
    assert "DEBUG" in clean

    # Values are never in the scrubbed list
    assert "s3cr3t" not in scrubbed
    assert "hunter2" not in scrubbed
    assert "ghp_xxx" not in scrubbed


def test_scrubbed_keys_not_in_child_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAND-7: child process env never contains scrubbed keys (via ExecDisabled check)."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    cwd = str(tmp_path / "allowed")
    env = {"MY_TOKEN": "topsecret", "NORMAL": "ok"}

    result = run(["skcapstone", "status"], cwd=cwd, extra_env=env, config=cfg)
    assert isinstance(result, ExecDisabled)
    assert "MY_TOKEN" in result.scrubbed_keys
    assert "NORMAL" not in result.scrubbed_keys


# ---------------------------------------------------------------------------
# Non-existent cwd
# ---------------------------------------------------------------------------


def test_nonexistent_cwd_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cwd that does not exist → ValidationError(outcome='error')."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    ghost = str(tmp_path / "allowed" / "ghost_dir_that_does_not_exist")

    with pytest.raises(ValidationError) as exc_info:
        run(["skcapstone", "status"], cwd=ghost, extra_env={}, config=cfg)
    assert exc_info.value.outcome == "error"


# ---------------------------------------------------------------------------
# Empty argv
# ---------------------------------------------------------------------------


def test_empty_argv_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty argv raises ValidationError before any other check."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    cwd = str(tmp_path / "allowed")

    with pytest.raises(ValidationError):
        run([], cwd=cwd, extra_env={}, config=cfg)


# ---------------------------------------------------------------------------
# Allowed cwd passes validation
# ---------------------------------------------------------------------------


def test_allowed_cwd_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cwd inside allowed_cwd passes validation and returns ExecDisabled."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cfg = _make_config(tmp_path)
    cwd = str(tmp_path / "allowed")

    result = run(["skcapstone", "status"], cwd=cwd, extra_env={}, config=cfg)
    assert isinstance(result, ExecDisabled)
    assert result.argv == ["skcapstone", "status"]
