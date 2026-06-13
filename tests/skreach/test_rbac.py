"""Tests for skchat.skreach.rbac — RBAC matrix.

Covers acceptance criteria:
  RBAC-1: guest role → denied for all command classes
  RBAC-2: member role → denied for exec/deploy/destructive
  RBAC-3: agent role → denied for exec without per-node grant; allowed with grant
  RBAC-4: no self-elevation (role is resolved server-side, not from envelope)
  + owner → all allowed (destructive needs confirm)
  + operator → all except owner-class; destructive needs confirm
  + confirm_required returned for destructive (not denied)
"""

from __future__ import annotations

import pytest

from skchat.skreach.rbac import (
    CommandClass,
    Decision,
    DecisionKind,
    NodeGrants,
    Role,
    authorize,
)


# ---------------------------------------------------------------------------
# RBAC-1: Guest is denied everything
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd_class",
    [
        CommandClass.STATUS,
        CommandClass.LOG_READ,
        CommandClass.FILE_READ,
        CommandClass.FILE_WRITE,
        CommandClass.EXEC,
        CommandClass.DEPLOY,
        CommandClass.DESTRUCTIVE,
        CommandClass.OWNER,
    ],
)
def test_guest_denied_all_classes(cmd_class: CommandClass) -> None:
    """RBAC-1: guest role cannot issue any command class."""
    d = authorize(role="guest", cmd_class=cmd_class.value)
    assert d.denied, f"guest should be denied {cmd_class}, got {d}"


# ---------------------------------------------------------------------------
# RBAC-2: Member is read-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd_class",
    [CommandClass.STATUS, CommandClass.LOG_READ, CommandClass.FILE_READ],
)
def test_member_allowed_read_classes(cmd_class: CommandClass) -> None:
    """RBAC-2: member can issue status/log_read/file_read."""
    d = authorize(role="member", cmd_class=cmd_class.value)
    assert d.allowed, f"member should be allowed {cmd_class}, got {d}"


@pytest.mark.parametrize(
    "cmd_class",
    [
        CommandClass.FILE_WRITE,
        CommandClass.EXEC,
        CommandClass.DEPLOY,
        CommandClass.DESTRUCTIVE,
        CommandClass.OWNER,
    ],
)
def test_member_denied_exec_and_write_classes(cmd_class: CommandClass) -> None:
    """RBAC-2: member cannot issue exec/deploy/destructive/file_write/owner."""
    d = authorize(role="member", cmd_class=cmd_class.value)
    assert d.denied, f"member should be denied {cmd_class}, got {d}"


# ---------------------------------------------------------------------------
# RBAC-3: Agent base restrictions, explicit grant unlock
# ---------------------------------------------------------------------------


def test_agent_allowed_status_no_grant() -> None:
    """RBAC-3: agent can issue status without a per-node grant."""
    d = authorize(role="agent", cmd_class="status")
    assert d.allowed


def test_agent_allowed_log_read_no_grant() -> None:
    """RBAC-3: agent can issue log_read without a per-node grant."""
    d = authorize(role="agent", cmd_class="log_read")
    assert d.allowed


def test_agent_denied_exec_without_grant() -> None:
    """RBAC-3: agent exec without a per-node grant → denied."""
    d = authorize(
        role="agent",
        cmd_class="exec",
        issuer_fqid="lumina@chef.skworld.io",
        node_grants=NodeGrants(grants=[]),  # empty grants
    )
    assert d.denied


def test_agent_exec_with_grant_returns_confirm_required(
    node_grants_with_agent_exec,
) -> None:
    """RBAC-3: agent with explicit exec grant → confirm_required (§2.3)."""
    d = authorize(
        role="agent",
        cmd_class="exec",
        issuer_fqid="lumina@chef.skworld.io",
        node_grants=node_grants_with_agent_exec,
    )
    assert d.confirm_required, f"agent exec grant should require confirm, got {d}"
    assert not d.denied


def test_agent_exec_grant_for_different_fqid_still_denied(
    node_grants_with_agent_exec,
) -> None:
    """RBAC-3: exec grant for lumina does not help opus."""
    d = authorize(
        role="agent",
        cmd_class="exec",
        issuer_fqid="opus@chef.skworld.io",  # NOT lumina
        node_grants=node_grants_with_agent_exec,
    )
    assert d.denied


# ---------------------------------------------------------------------------
# RBAC-4: No self-elevation (role comes from server; envelope claims ignored)
# ---------------------------------------------------------------------------


def test_member_cannot_elevate_to_operator() -> None:
    """RBAC-4: passing role='operator' for a member is purely the caller's logic.

    The spec says the role is resolved server-side before authorize() is called.
    This test verifies that if we call authorize() with 'member' we get member
    semantics regardless of what the envelope body might claim.
    """
    # The envelope body has a fabricated 'role=operator' claim — but skreachd
    # resolves the role server-side and passes 'member' here.
    d = authorize(role="member", cmd_class="exec")
    assert d.denied
    # Contrast: if the server resolved 'operator', exec would be allowed
    d2 = authorize(role="operator", cmd_class="exec")
    assert d2.allowed


# ---------------------------------------------------------------------------
# Owner: all classes allowed (destructive → confirm)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd_class",
    [
        CommandClass.STATUS,
        CommandClass.LOG_READ,
        CommandClass.FILE_READ,
        CommandClass.FILE_WRITE,
        CommandClass.EXEC,
        CommandClass.DEPLOY,
        CommandClass.OWNER,
    ],
)
def test_owner_allowed_all_non_destructive(cmd_class: CommandClass) -> None:
    """Owner can issue any non-destructive class."""
    d = authorize(role="owner", cmd_class=cmd_class.value)
    assert d.allowed, f"owner should be allowed {cmd_class}, got {d}"


def test_owner_destructive_requires_confirm() -> None:
    """Owner's destructive commands still require a second signed confirm (§3.3)."""
    d = authorize(role="owner", cmd_class="destructive")
    assert d.confirm_required
    assert not d.denied


# ---------------------------------------------------------------------------
# Operator: all classes except owner; destructive → confirm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd_class",
    [
        CommandClass.STATUS,
        CommandClass.LOG_READ,
        CommandClass.FILE_READ,
        CommandClass.FILE_WRITE,
        CommandClass.EXEC,
        CommandClass.DEPLOY,
    ],
)
def test_operator_allowed_non_destructive_non_owner(cmd_class: CommandClass) -> None:
    """Operator can issue all classes except owner-class and destructive."""
    d = authorize(role="operator", cmd_class=cmd_class.value)
    assert d.allowed, f"operator should be allowed {cmd_class}, got {d}"


def test_operator_denied_owner_class() -> None:
    """Operator cannot issue owner-class commands."""
    d = authorize(role="operator", cmd_class="owner")
    assert d.denied


def test_operator_destructive_requires_confirm() -> None:
    """Operator's destructive commands require confirm (§3.3)."""
    d = authorize(role="operator", cmd_class="destructive")
    assert d.confirm_required
    assert not d.denied


# ---------------------------------------------------------------------------
# Edge cases: unknown role / unknown class
# ---------------------------------------------------------------------------


def test_unknown_role_denied() -> None:
    """An unknown role string is denied."""
    d = authorize(role="superadmin", cmd_class="status")
    assert d.denied


def test_unknown_class_denied() -> None:
    """An unknown command class string is denied."""
    d = authorize(role="operator", cmd_class="teleport")
    assert d.denied
