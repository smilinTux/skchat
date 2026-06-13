"""RBAC matrix — F1 §2 & §3.

Defines:
  - Role: the four trust tiers (owner/operator/member/agent/guest).
  - CommandClass: the eight command-risk tiers.
  - Decision: the authorization outcome.
  - authorize(): evaluates (role, command_class, node_grants) → Decision.

The role is always resolved server-side from the issuer FQID; no envelope
claim overrides it.  See §2.2 and §3.1 of the F1 spec.

Spec: docs/superpowers/specs/2026-06-13-skreach-security.md §2–§3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """Trust tiers for skreach principals (§2.2)."""

    OWNER = "owner"
    OPERATOR = "operator"
    MEMBER = "member"
    AGENT = "agent"
    GUEST = "guest"


class CommandClass(str, Enum):
    """Risk-tier groupings for skreach commands (§2.1)."""

    STATUS = "status"
    LOG_READ = "log_read"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    EXEC = "exec"
    DEPLOY = "deploy"
    DESTRUCTIVE = "destructive"
    OWNER = "owner"  # node registration / key rotation / skreachd config reload


class DecisionKind(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM_REQUIRED = "confirm_required"


@dataclass
class Decision:
    """Authorization decision returned by authorize().

    Attributes:
        kind:    Whether the command is allowed, denied, or needs a confirm.
        reason:  Human-readable explanation (for audit records and logs).
        confirm_required: Convenience bool; True when kind==CONFIRM_REQUIRED.
    """

    kind: DecisionKind
    reason: str
    confirm_required: bool = False

    @classmethod
    def allow(cls, reason: str = "") -> "Decision":
        return cls(kind=DecisionKind.ALLOW, reason=reason or "authorized")

    @classmethod
    def deny(cls, reason: str) -> "Decision":
        return cls(kind=DecisionKind.DENY, reason=reason)

    @classmethod
    def confirm(cls, reason: str = "") -> "Decision":
        return cls(
            kind=DecisionKind.CONFIRM_REQUIRED,
            reason=reason or "confirm required for destructive op",
            confirm_required=True,
        )

    @property
    def allowed(self) -> bool:
        return self.kind == DecisionKind.ALLOW

    @property
    def denied(self) -> bool:
        return self.kind == DecisionKind.DENY


# ---------------------------------------------------------------------------
# Per-node agent grant (§2.3)
# ---------------------------------------------------------------------------


@dataclass
class AgentGrant:
    """Explicit per-node exec grant for an agent FQID (§2.3).

    These are configured in skreach-node.yaml, not self-granted at runtime.
    """

    fqid: str
    classes: list[str] = field(default_factory=list)  # e.g. ["exec"]
    allowlist: list[str] = field(default_factory=list)  # binary names only
    require_confirm: bool = True  # agents always require confirm (§2.3)


@dataclass
class NodeGrants:
    """Collection of per-node grants, loaded from skreach-node.yaml."""

    grants: list[AgentGrant] = field(default_factory=list)

    def agent_has_grant(self, fqid: str, cmd_class: str) -> bool:
        """Return True if fqid has an explicit grant for cmd_class on this node."""
        for g in self.grants:
            if g.fqid == fqid and cmd_class in g.classes:
                return True
        return False


# ---------------------------------------------------------------------------
# Role → allowed command-class table (§2.2)
# ---------------------------------------------------------------------------

# Member read-only set
_MEMBER_CLASSES = frozenset(
    [CommandClass.STATUS, CommandClass.LOG_READ, CommandClass.FILE_READ]
)

# Agent base set (before per-node grants)
_AGENT_BASE_CLASSES = frozenset([CommandClass.STATUS, CommandClass.LOG_READ])

# Operator set (all except owner-class)
_OPERATOR_CLASSES = frozenset(
    [
        CommandClass.STATUS,
        CommandClass.LOG_READ,
        CommandClass.FILE_READ,
        CommandClass.FILE_WRITE,
        CommandClass.EXEC,
        CommandClass.DEPLOY,
        CommandClass.DESTRUCTIVE,
    ]
)


# ---------------------------------------------------------------------------
# Main authorization function (§3.1)
# ---------------------------------------------------------------------------


def authorize(
    role: str,
    cmd_class: str,
    *,
    issuer_fqid: str = "",
    node_grants: Optional[NodeGrants] = None,
) -> Decision:
    """Evaluate whether *role* may issue *cmd_class* on this node.

    Implements the ordered policy from §3.2:
      1. Default-deny.
      2. Role floor (§2.2 table).
      3. Destructive-confirm gate (§3.3).

    NOTE: allowlist and denylist checks are handled by sandbox.py (§3.2 steps
    3 and 4); this function focuses on the role-class matrix.

    Args:
        role:          Role string from the server-resolved identity.
        cmd_class:     CommandClass string from the envelope.
        issuer_fqid:   The issuer FQID (used for agent grant lookups).
        node_grants:   Per-node grant config (§2.3); defaults to empty grants.

    Returns:
        Decision — allow / deny / confirm_required.
    """
    if node_grants is None:
        node_grants = NodeGrants()

    # Normalise inputs
    try:
        r = Role(role)
    except ValueError:
        return Decision.deny(f"unknown role '{role}'")

    try:
        cc = CommandClass(cmd_class)
    except ValueError:
        return Decision.deny(f"unknown command class '{cmd_class}'")

    # --- Guest: never ---
    if r == Role.GUEST:
        return Decision.deny("guest principals have no skreach access")

    # --- Owner: always (destructive still needs confirm at exec time) ---
    if r == Role.OWNER:
        if cc == CommandClass.DESTRUCTIVE:
            return Decision.confirm("destructive command requires confirm even for owner")
        return Decision.allow("owner role; all classes permitted")

    # --- Operator: all classes except owner ---
    if r == Role.OPERATOR:
        if cc == CommandClass.OWNER:
            return Decision.deny("operator cannot issue owner-class commands")
        if cc == CommandClass.DESTRUCTIVE:
            return Decision.confirm("destructive command requires confirm")
        return Decision.allow("operator role; class permitted")

    # --- Member: read-only ---
    if r == Role.MEMBER:
        if cc in _MEMBER_CLASSES:
            return Decision.allow("member role; read-only class permitted")
        return Decision.deny(
            f"member role cannot issue '{cmd_class}' commands (read-only)"
        )

    # --- Agent: status + scoped log_read, plus explicit per-node grants ---
    if r == Role.AGENT:
        if cc in _AGENT_BASE_CLASSES:
            # Agents always need confirm for any op (§2.3) — but base classes
            # (status/log_read) are allowed without confirm as they are read-only.
            return Decision.allow("agent role; base read class permitted")
        # Check per-node grant
        if node_grants.agent_has_grant(issuer_fqid, cmd_class):
            # Agent exec grants ALWAYS require confirm (§2.3)
            return Decision.confirm(
                f"agent granted '{cmd_class}' on this node; confirm required"
            )
        return Decision.deny(
            f"agent role cannot issue '{cmd_class}' without an explicit per-node grant"
        )

    # Should never reach here (Role enum is exhaustive)
    return Decision.deny(f"unhandled role '{role}'")
