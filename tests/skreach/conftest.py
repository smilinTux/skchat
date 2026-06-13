"""Shared fixtures for skreach tests.

All tests are fully offline — no real capauth, no real PGP, no real network,
no real subprocess.  Injectable fakes substitute for every external dependency.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Optional

import pytest

from skchat.skreach.protocol import (
    CmdPayload,
    CommandEnvelope,
    _ReplayCache,
)
from skchat.skreach.rbac import AgentGrant, NodeGrants
from skchat.skreach.sandbox import SandboxConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    *,
    cmd_id: Optional[str] = None,
    iss: str = "chef@skworld.io",
    sub: str = "noroc2027@chef.skworld.io",
    iat: Optional[float] = None,
    exp: Optional[float] = None,
    cls: str = "status",
    op: str = "health",
    args: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    cwd: str = "/opt/skworld",
) -> CommandEnvelope:
    """Build a CommandEnvelope with sensible defaults."""
    now = time.time()
    return CommandEnvelope(
        id=cmd_id or secrets.token_hex(16),
        iss=iss,
        sub=sub,
        iat=iat if iat is not None else now,
        exp=exp if exp is not None else now + 120,
        cmd=CmdPayload(
            cls=cls,
            op=op,
            args=args or ["skcapstone", "status"],
            env=env or {},
            cwd=cwd,
        ),
    )


# ---------------------------------------------------------------------------
# Verifier / role-resolver fakes
# ---------------------------------------------------------------------------


def _sig_always_valid(_env: CommandEnvelope) -> bool:
    """Always returns True — valid signature."""
    return True


def _sig_always_invalid(_env: CommandEnvelope) -> bool:
    """Always returns False — invalid signature."""
    return False


def _role_owner(_iss: str) -> str:
    return "owner"


def _role_operator(_iss: str) -> str:
    return "operator"


def _role_member(_iss: str) -> str:
    return "member"


def _role_agent(_iss: str) -> str:
    return "agent"


def _role_guest(_iss: str) -> str:
    return "guest"


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_replay_cache() -> _ReplayCache:
    """A fresh, empty replay cache (isolated per test)."""
    return _ReplayCache()


@pytest.fixture
def valid_envelope() -> CommandEnvelope:
    """A fresh, valid CommandEnvelope (status/health, owner issuer)."""
    return _make_envelope()


@pytest.fixture
def sandbox_config_tmp(tmp_path: Path) -> SandboxConfig:
    """SandboxConfig whose allowed_cwd points at a real temp directory."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    return SandboxConfig(
        allowed_cwd=[str(allowed)],
        command_denylist=[],
        wall_clock_timeout=5.0,
        node_fqid="noroc2027@chef.skworld.io",
        skreach_home=str(tmp_path),
    )


@pytest.fixture
def node_grants_with_agent_exec() -> NodeGrants:
    """NodeGrants giving lumina@chef.skworld.io exec rights (§2.3)."""
    return NodeGrants(
        grants=[
            AgentGrant(
                fqid="lumina@chef.skworld.io",
                classes=["exec"],
                allowlist=["skcapstone", "skingest"],
                require_confirm=True,
            )
        ]
    )
