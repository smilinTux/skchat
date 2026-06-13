"""Declarative-ops wrapper for skcapstone trustee primitives — F4.

TrusteeOps is a facade that maps deploy/status/restart/scale/logs/health
operations onto skcapstone MCP primitives (trustee_*, deploy_status,
run_ansible_playbook, coord_*, itil_*).  Every operation:

  1. Goes through RBAC authorize() — deploy-class for read ops, destructive
     for write ops (restart/scale-down).
  2. Is audited via AuditWriter before the MCP call is dispatched.
  3. Uses an **injectable MCP callable** so the entire class is unit-testable
     without touching real ops (no skcapstone MCP connection required).

Design intent (§2.6 / F4):
  - The actual skcapstone MCP is a thin callable; TrusteeOps owns the RBAC +
    audit envelope around every call.
  - Confirm-on-destructive: scale-down (count < current) and restart always
    return confirm_required unless a confirm_token is supplied.
  - Nothing is executed live in this F4 pass; exec_enabled is deliberately
    not wired to TrusteeOps (trustee calls are not subprocesses).

Spec: docs/superpowers/specs/2026-06-12-skchat-architecture-reassessment.md §2.6
      docs/superpowers/specs/2026-06-13-skreach-security.md §2–§4
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .audit import AuditRecord, AuditWriter
from .rbac import CommandClass, authorize

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP callable type
# ---------------------------------------------------------------------------

# A callable that wraps one skcapstone MCP tool call.
# Signature: (tool_name: str, **kwargs) -> dict[str, Any]
# In production: thin client calling the skcapstone MCP server.
# In tests: a fake/recording callable.
McpCallable = Callable[..., dict[str, Any]]


# ---------------------------------------------------------------------------
# Op result
# ---------------------------------------------------------------------------


@dataclass
class OpResult:
    """Result of a TrusteeOps operation.

    Attributes:
        ok:               True iff the op completed (or was dispatched) successfully.
        outcome:          Short outcome string — matches AuditRecord.outcome values.
        data:             The payload returned by the MCP primitive (or {}).
        audit_id:         The AuditRecord id for this op.
        confirm_required: True when RBAC requires a confirm_token to proceed.
        deny_reason:      Non-empty when RBAC denied the operation outright.
        error:            Non-empty on unexpected errors.
    """

    ok: bool
    outcome: str
    data: dict[str, Any] = field(default_factory=dict)
    audit_id: str = ""
    confirm_required: bool = False
    deny_reason: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Default production MCP stub (placeholder — wire real client in F5+)
# ---------------------------------------------------------------------------


def _default_mcp_call(tool_name: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
    """Default no-op MCP callable.

    In production this will be replaced with a thin client that calls the
    skcapstone MCP server over stdio/SSE.  For now it returns a stub
    response so TrusteeOps is import-safe and testable.
    """
    logger.warning(
        "TrusteeOps: no real MCP client wired; tool=%s kwargs=%r (stub response)",
        tool_name,
        kwargs,
    )
    return {"status": "stub", "tool": tool_name, "kwargs": kwargs}


# ---------------------------------------------------------------------------
# TrusteeOps facade
# ---------------------------------------------------------------------------

# CommandClass used for each operation type (maps to RBAC policy).
_CLASS_FOR_OP: dict[str, str] = {
    # Read-only / status ops → deploy class (operator can read; agent base read allowed)
    "deploy_status": CommandClass.STATUS,
    "health": CommandClass.STATUS,
    "logs": CommandClass.LOG_READ,
    # Write ops → deploy class (operator allowed, member denied)
    "restart": CommandClass.DEPLOY,
    "scale": CommandClass.DEPLOY,
    # Potentially destructive write ops that also need confirm
    "restart_destructive": CommandClass.DESTRUCTIVE,
    "scale_down": CommandClass.DESTRUCTIVE,
}


class TrusteeOps:
    """Declarative-ops wrapper: every op = RBAC check → audit → MCP dispatch.

    Args:
        issuer_role:    The issuer's resolved role string (owner/operator/…).
        issuer_fqid:    The issuer's FQID (for agent grant lookups + audit).
        mcp_call:       Injectable MCP callable; defaults to a no-op stub.
        audit_writer:   AuditWriter instance; creates a default writer if None.
        node_fqid:      The node FQID (for audit records).
    """

    def __init__(
        self,
        *,
        issuer_role: str,
        issuer_fqid: str = "",
        mcp_call: Optional[McpCallable] = None,
        audit_writer: Optional[AuditWriter] = None,
        node_fqid: str = "",
    ) -> None:
        self._role = issuer_role
        self._fqid = issuer_fqid
        self._mcp = mcp_call if mcp_call is not None else _default_mcp_call
        self._audit = audit_writer or AuditWriter()
        self._node_fqid = node_fqid

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rbac_and_audit(
        self,
        *,
        cmd_class: str,
        op: str,
        argv: list[str],
        confirm_token: Optional[str] = None,
    ) -> tuple[bool, OpResult]:
        """Run RBAC + audit for one op.

        Returns (proceed: bool, result_on_block).  When proceed=True the
        caller should dispatch the MCP call.  When False the returned OpResult
        is the final answer.

        Confirm-on-destructive: if RBAC returns confirm_required and no
        confirm_token is supplied, block with confirm_required outcome.
        If a confirm_token IS supplied for a destructive op, promote the
        class to DEPLOY and re-evaluate (allows through at operator tier).
        """
        decision = authorize(
            role=self._role,
            cmd_class=cmd_class,
            issuer_fqid=self._fqid,
        )

        if decision.denied:
            rec = self._audit.write_rejection(
                outcome="rbac_denied",
                iss_fqid=self._fqid,
                role=self._role,
                cmd_class=cmd_class,
                op=op,
                argv=argv,
            )
            return False, OpResult(
                ok=False,
                outcome="rbac_denied",
                audit_id=rec.audit_id,
                deny_reason=decision.reason,
            )

        if decision.confirm_required and not confirm_token:
            rec = self._audit.write_rejection(
                outcome="confirm_required",
                iss_fqid=self._fqid,
                role=self._role,
                cmd_class=cmd_class,
                op=op,
                argv=argv,
            )
            return False, OpResult(
                ok=False,
                outcome="confirm_required",
                audit_id=rec.audit_id,
                confirm_required=True,
                deny_reason=decision.reason,
            )

        # Allowed (possibly with confirm token for destructive) — write pre-op audit
        audit_rec = AuditRecord(
            node_fqid=self._node_fqid,
            iss_fqid=self._fqid,
            role=self._role,
            cmd_class=cmd_class,
            op=op,
            argv=argv,
            started_at=time.time(),
        )
        audit_rec.outcome = "dispatching"
        self._audit.write(audit_rec)
        return True, OpResult(ok=True, outcome="dispatching", audit_id=audit_rec.audit_id)

    def _dispatch(
        self,
        tool: str,
        op: str,
        argv: list[str],
        cmd_class: str,
        confirm_token: Optional[str],
        **mcp_kwargs: Any,  # noqa: ANN401
    ) -> OpResult:
        """Full pipeline: RBAC → audit → MCP dispatch → final audit."""
        proceed, early = self._rbac_and_audit(
            cmd_class=cmd_class,
            op=op,
            argv=argv,
            confirm_token=confirm_token,
        )
        if not proceed:
            return early

        # MCP call
        try:
            data = self._mcp(tool, **mcp_kwargs)
            outcome = "dispatched"
        except Exception as exc:  # noqa: BLE001
            logger.error("TrusteeOps: MCP call failed tool=%s: %r", tool, exc)
            # Write failure audit
            rec = self._audit.write_rejection(
                outcome="error",
                iss_fqid=self._fqid,
                role=self._role,
                cmd_class=cmd_class,
                op=op,
                argv=argv,
            )
            return OpResult(
                ok=False,
                outcome="error",
                audit_id=rec.audit_id,
                error=str(exc),
            )

        # Post-dispatch audit
        post_rec = AuditRecord(
            node_fqid=self._node_fqid,
            iss_fqid=self._fqid,
            role=self._role,
            cmd_class=cmd_class,
            op=op,
            argv=argv,
        )
        post_rec.finalise(outcome=outcome)
        self._audit.write(post_rec)

        return OpResult(
            ok=True,
            outcome=outcome,
            data=data,
            audit_id=post_rec.audit_id,
        )

    # ------------------------------------------------------------------
    # Public API — one method per deploy-plane op
    # ------------------------------------------------------------------

    def deploy_status(self, service: str) -> OpResult:
        """Query deploy status for *service* via skcapstone deploy_status.

        Maps to CommandClass.STATUS (read-only; operator/member/agent allowed).
        """
        return self._dispatch(
            tool="deploy_status",
            op="deploy_status",
            argv=["deploy_status", service],
            cmd_class=CommandClass.STATUS,
            confirm_token=None,
            service=service,
        )

    def health(self, service: str) -> OpResult:
        """Query trustee health for *service* via trustee_health.

        Maps to CommandClass.STATUS (read-only).
        """
        return self._dispatch(
            tool="trustee_health",
            op="health",
            argv=["trustee_health", service],
            cmd_class=CommandClass.STATUS,
            confirm_token=None,
            service=service,
        )

    def logs(
        self,
        service: str,
        *,
        tail: int = 50,
        since: Optional[str] = None,
    ) -> OpResult:
        """Fetch recent logs for *service* via trustee_logs.

        Maps to CommandClass.LOG_READ (read-only; operator/member/agent allowed).
        """
        argv = ["trustee_logs", service, f"--tail={tail}"]
        if since:
            argv.append(f"--since={since}")
        return self._dispatch(
            tool="trustee_logs",
            op="logs",
            argv=argv,
            cmd_class=CommandClass.LOG_READ,
            confirm_token=None,
            service=service,
            tail=tail,
            since=since,
        )

    def restart(
        self,
        service: str,
        *,
        confirm_token: Optional[str] = None,
    ) -> OpResult:
        """Restart *service* via trustee_restart.

        Restart is DESTRUCTIVE — requires confirm_token unless the caller
        supplies one (confirms they understand the impact).

        Maps to CommandClass.DESTRUCTIVE → confirm_required for operator;
        denied for member/agent without explicit grant.
        """
        return self._dispatch(
            tool="trustee_restart",
            op="restart",
            argv=["trustee_restart", service],
            cmd_class=CommandClass.DESTRUCTIVE,
            confirm_token=confirm_token,
            service=service,
        )

    def scale(
        self,
        service: str,
        replicas: int,
        *,
        current_replicas: Optional[int] = None,
        confirm_token: Optional[str] = None,
    ) -> OpResult:
        """Scale *service* to *replicas* via trustee_scale.

        Scale-DOWN (replicas < current_replicas) is DESTRUCTIVE.
        Scale-UP uses CommandClass.DEPLOY.

        If current_replicas is not supplied, treats as a deploy-class op
        (safer default — the caller should supply it for proper guard).

        Args:
            service:          Service name.
            replicas:         Desired replica count.
            current_replicas: Current replica count (for scale-down detection).
            confirm_token:    Required for scale-down (destructive path).
        """
        is_scale_down = (
            current_replicas is not None and replicas < current_replicas
        )
        cmd_class = CommandClass.DESTRUCTIVE if is_scale_down else CommandClass.DEPLOY

        return self._dispatch(
            tool="trustee_scale",
            op="scale",
            argv=["trustee_scale", service, str(replicas)],
            cmd_class=cmd_class,
            confirm_token=confirm_token,
            service=service,
            replicas=replicas,
        )

    def run_ansible(
        self,
        playbook: str,
        *,
        extra_vars: Optional[dict[str, str]] = None,
        confirm_token: Optional[str] = None,
    ) -> OpResult:
        """Run an Ansible playbook via run_ansible_playbook.

        Ansible runs are DEPLOY-class (potentially service-impacting).
        """
        argv = ["run_ansible_playbook", playbook]
        return self._dispatch(
            tool="run_ansible_playbook",
            op="run_ansible",
            argv=argv,
            cmd_class=CommandClass.DEPLOY,
            confirm_token=confirm_token,
            playbook=playbook,
            extra_vars=extra_vars or {},
        )

    def coord_status(self, task_id: Optional[str] = None) -> OpResult:
        """Check coordination task status via coord_status.

        Maps to CommandClass.STATUS (read-only).
        """
        argv = ["coord_status"]
        if task_id:
            argv.append(task_id)
        return self._dispatch(
            tool="coord_status",
            op="coord_status",
            argv=argv,
            cmd_class=CommandClass.STATUS,
            confirm_token=None,
            task_id=task_id,
        )

    def itil_incident_list(self) -> OpResult:
        """List active ITIL incidents via itil_incident_list.

        Maps to CommandClass.STATUS (read-only).
        """
        return self._dispatch(
            tool="itil_incident_list",
            op="itil_incident_list",
            argv=["itil_incident_list"],
            cmd_class=CommandClass.STATUS,
            confirm_token=None,
        )
