"""Tests for skchat.skreach.daemon — skreachd dispatch pipeline.

Covers the end-to-end F1 pipeline through Skreachd.handle_one():
  - Bad signature → sig_invalid, no exec
  - Expired envelope → expired_cmd, no exec
  - Replay → replay_cmd, no exec
  - RBAC: member tries exec → rbac_denied, no exec
  - RBAC: guest → denied, no exec (guest drops at verify step)
  - Destructive command → confirm_required (not executed)
  - Bad cwd → allowlist_denied, no exec (ValidationError path)
  - Happy path: valid operator+status → exec_disabled (exec gated off)
  - Audit records are written for every outcome
  - exec DISABLED: with exec enabled=0, subprocess.Popen never called (across all paths)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest

from skchat.skreach.audit import AuditWriter
from skchat.skreach.daemon import Skreachd
from skchat.skreach.protocol import _ReplayCache
from skchat.skreach.rbac import NodeGrants
from skchat.skreach.sandbox import ExecDisabled, SandboxConfig

from .conftest import (
    _make_envelope,
    _role_guest,
    _role_member,
    _role_operator,
    _role_owner,
    _sig_always_invalid,
    _sig_always_valid,
)

SELF_FQID = "noroc2027@chef.skworld.io"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_daemon(
    tmp_path: Path,
    *,
    role_resolver=_role_operator,
    sig_verifier=_sig_always_valid,
    node_grants: Optional[NodeGrants] = None,
    sandbox_config: Optional[SandboxConfig] = None,
    replay_cache: Optional[_ReplayCache] = None,
) -> tuple[Skreachd, Path]:
    """Build a test Skreachd wired to a tmp audit file."""
    audit_path = tmp_path / "audit.jsonl"
    writer = AuditWriter(jsonl_path=audit_path)

    if sandbox_config is None:
        allowed = tmp_path / "allowed"
        allowed.mkdir(exist_ok=True)
        sandbox_config = SandboxConfig(
            allowed_cwd=[str(allowed)],
            node_fqid=SELF_FQID,
            skreach_home=str(tmp_path),
        )

    daemon = Skreachd(
        self_fqid=SELF_FQID,
        sig_verifier=sig_verifier,
        role_resolver=role_resolver,
        audit_writer=writer,
        node_grants=node_grants or NodeGrants(),
        sandbox_config=sandbox_config,
        replay_cache=replay_cache or _ReplayCache(),
    )
    return daemon, audit_path


def _read_audit(path: Path) -> list[dict]:
    import json

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().split("\n") if line.strip()]


# ---------------------------------------------------------------------------
# Bad signature → sig_invalid
# ---------------------------------------------------------------------------


def test_bad_signature_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    daemon, audit_path = _make_daemon(tmp_path, sig_verifier=_sig_always_invalid)
    env = _make_envelope()

    result = daemon.handle_one(env)

    assert result.outcome == "sig_invalid"
    records = _read_audit(audit_path)
    assert any(r["outcome"] == "sig_invalid" for r in records)


# ---------------------------------------------------------------------------
# Expired envelope → expired_cmd
# ---------------------------------------------------------------------------


def test_expired_envelope_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    now = time.time()
    daemon, audit_path = _make_daemon(tmp_path)
    env = _make_envelope(iat=now - 400, exp=now - 50)

    result = daemon.handle_one(env)

    assert result.outcome == "expired_cmd"
    records = _read_audit(audit_path)
    assert any(r["outcome"] == "expired_cmd" for r in records)


# ---------------------------------------------------------------------------
# Replay → replay_cmd
# ---------------------------------------------------------------------------


def test_replay_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    cache = _ReplayCache()
    daemon, audit_path = _make_daemon(tmp_path, replay_cache=cache)
    # Use a cwd that exists (the tmp allowed dir)
    allowed = tmp_path / "allowed"
    env = _make_envelope(cls="status", op="health", cwd=str(allowed))

    r1 = daemon.handle_one(env)
    assert r1.outcome in ("executed", "exec_disabled"), (
        f"Expected exec_disabled or executed on first delivery, got {r1.outcome}: {r1.error}"
    )

    r2 = daemon.handle_one(env)
    assert r2.outcome == "replay_cmd"


# ---------------------------------------------------------------------------
# Member tries exec → rbac_denied
# ---------------------------------------------------------------------------


def test_member_exec_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    daemon, audit_path = _make_daemon(tmp_path, role_resolver=_role_member)
    allowed = tmp_path / "allowed"
    env = _make_envelope(cls="exec", op="run", cwd=str(allowed))

    result = daemon.handle_one(env)

    assert result.outcome == "rbac_denied"
    records = _read_audit(audit_path)
    assert any(r["outcome"] == "rbac_denied" for r in records)


# ---------------------------------------------------------------------------
# Guest role → unauthorized_iss (verify rejects before RBAC)
# ---------------------------------------------------------------------------


def test_guest_issuer_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    daemon, audit_path = _make_daemon(tmp_path, role_resolver=_role_guest)
    env = _make_envelope()

    result = daemon.handle_one(env)
    assert result.outcome == "unauthorized_iss"


# ---------------------------------------------------------------------------
# Destructive command → confirm_required
# ---------------------------------------------------------------------------


def test_destructive_returns_confirm_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    daemon, audit_path = _make_daemon(tmp_path, role_resolver=_role_operator)
    env = _make_envelope(cls="destructive", op="stop")

    result = daemon.handle_one(env)

    assert result.outcome == "confirm_required"
    records = _read_audit(audit_path)
    assert any(r["outcome"] == "confirm_required" for r in records)


# ---------------------------------------------------------------------------
# Owner destructive also returns confirm_required
# ---------------------------------------------------------------------------


def test_owner_destructive_confirm_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    daemon, audit_path = _make_daemon(tmp_path, role_resolver=_role_owner)
    env = _make_envelope(cls="destructive", op="scale-down")

    result = daemon.handle_one(env)
    assert result.outcome == "confirm_required"


# ---------------------------------------------------------------------------
# Bad cwd → allowlist_denied / error (ValidationError path)
# ---------------------------------------------------------------------------


def test_bad_cwd_triggers_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    daemon, audit_path = _make_daemon(tmp_path)
    env = _make_envelope(cls="exec", op="run", cwd="/etc")  # outside allowed_cwd

    result = daemon.handle_one(env)
    assert result.outcome in ("allowlist_denied", "error")


# ---------------------------------------------------------------------------
# Happy path: valid operator + status → exec_disabled
# ---------------------------------------------------------------------------


def test_valid_operator_status_exec_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    daemon, audit_path = _make_daemon(tmp_path)
    allowed = tmp_path / "allowed"
    env = _make_envelope(cls="status", op="health", cwd=str(allowed))

    result = daemon.handle_one(env)

    assert result.outcome == "exec_disabled"
    assert isinstance(result.exec_out, ExecDisabled)


# ---------------------------------------------------------------------------
# EXEC DISABLED: subprocess.Popen never called across all paths
# ---------------------------------------------------------------------------


def test_subprocess_never_spawned_across_all_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With exec disabled, subprocess.Popen is never called regardless of outcome."""
    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)

    with mock.patch("subprocess.Popen") as mock_popen:
        cache = _ReplayCache()
        allowed = tmp_path / "allowed"
        allowed.mkdir(exist_ok=True)

        daemon, _ = _make_daemon(tmp_path, replay_cache=cache)

        # Trigger every outcome
        # sig_invalid
        bad_daemon, _ = _make_daemon(tmp_path, sig_verifier=_sig_always_invalid)
        bad_daemon.handle_one(_make_envelope())

        # expired
        now = time.time()
        daemon.handle_one(_make_envelope(exp=now - 100))

        # member exec → rbac_denied
        member_daemon, _ = _make_daemon(tmp_path, role_resolver=_role_member)
        member_daemon.handle_one(_make_envelope(cls="exec", cwd=str(allowed)))

        # destructive → confirm_required
        daemon.handle_one(_make_envelope(cls="destructive", cwd=str(allowed)))

        # valid status → exec_disabled
        daemon.handle_one(_make_envelope(cls="status", cwd=str(allowed)))

        # Assert: subprocess.Popen was NEVER called
        mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Audit: every outcome produces a record
# ---------------------------------------------------------------------------


def test_every_rejection_produces_audit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUDIT-2: every rejection path writes an audit record."""
    import json

    monkeypatch.delenv("SKREACH_EXEC_ENABLED", raising=False)
    audit_path = tmp_path / "audit.jsonl"
    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)

    def _make(role_fn, sig_fn=_sig_always_valid, **env_kw):
        writer = AuditWriter(jsonl_path=audit_path)
        cfg = SandboxConfig(
            allowed_cwd=[str(allowed)],
            node_fqid=SELF_FQID,
            skreach_home=str(tmp_path),
        )
        return Skreachd(
            self_fqid=SELF_FQID,
            sig_verifier=sig_fn,
            role_resolver=role_fn,
            audit_writer=writer,
            node_grants=NodeGrants(),
            sandbox_config=cfg,
            replay_cache=_ReplayCache(),
        )

    now = time.time()
    scenarios = [
        # (daemon, envelope) → expected outcome substring
        (
            _make(_role_operator, _sig_always_invalid),
            _make_envelope(),
            "sig_invalid",
        ),
        (
            _make(_role_guest),
            _make_envelope(),
            "unauthorized_iss",
        ),
        (
            _make(_role_member),
            _make_envelope(cls="exec", cwd=str(allowed)),
            "rbac_denied",
        ),
        (
            _make(_role_operator),
            _make_envelope(cls="destructive", cwd=str(allowed)),
            "confirm_required",
        ),
    ]

    outcomes_found = set()
    for d, env, expected in scenarios:
        d.handle_one(env)

    records = [json.loads(line) for line in audit_path.read_text().strip().split("\n") if line]
    outcome_set = {r["outcome"] for r in records}

    assert "sig_invalid" in outcome_set
    assert "unauthorized_iss" in outcome_set
    assert "rbac_denied" in outcome_set
    assert "confirm_required" in outcome_set
