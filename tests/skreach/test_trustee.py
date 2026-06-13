"""Tests for skchat.skreach.trustee — TrusteeOps declarative-ops wrapper.

All tests are fully offline — no real skcapstone MCP, no real network,
no real subprocess.  The MCP callable is an injected fake that records calls.

Covers:
  TRUSTEE-1: deploy_status / health / logs → STATUS/LOG_READ; authorized for
             operator, member, agent; denied for guest.
  TRUSTEE-2: restart → DESTRUCTIVE; confirm_required without token; allowed
             with token (operator); denied for member.
  TRUSTEE-3: scale-up → DEPLOY (no confirm needed for operator).
             scale-down → DESTRUCTIVE (confirm_required without token).
  TRUSTEE-4: run_ansible → DEPLOY class; operator allowed; member denied.
  TRUSTEE-5: Every op writes an audit record (dispatching or rejection).
  TRUSTEE-6: MCP callable is invoked for authorized ops; NOT called on RBAC deny.
  TRUSTEE-7: MCP callable exception → ok=False outcome="error".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from skchat.skreach.audit import AuditWriter
from skchat.skreach.trustee import OpResult, TrusteeOps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeMcp:
    """Records all calls and returns a configurable response."""

    def __init__(self, response: dict | None = None, raise_exc: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._response = response or {"status": "ok"}
        self._raise = raise_exc

    def __call__(self, tool_name: str, **kwargs: Any) -> dict:
        self.calls.append((tool_name, kwargs))
        if self._raise is not None:
            raise self._raise
        return self._response


def _ops(
    role: str,
    *,
    mcp: FakeMcp | None = None,
    audit_path: Path | None = None,
) -> TrusteeOps:
    writer = AuditWriter(jsonl_path=audit_path or Path("/dev/null"))
    return TrusteeOps(
        issuer_role=role,
        issuer_fqid=f"{role}-agent@skworld.io",
        mcp_call=mcp,
        audit_writer=writer,
        node_fqid="noroc2027@chef.skworld.io",
    )


# ---------------------------------------------------------------------------
# TRUSTEE-1: Read-only ops (status / health / logs)
# ---------------------------------------------------------------------------


class TestReadOps:
    def test_deploy_status_operator_ok(self):
        mcp = FakeMcp({"deploy": "running"})
        res = _ops("operator", mcp=mcp).deploy_status("skchat-daemon")
        assert res.ok
        assert res.outcome == "dispatched"
        assert len(mcp.calls) == 1
        assert mcp.calls[0][0] == "deploy_status"
        assert mcp.calls[0][1]["service"] == "skchat-daemon"

    def test_health_member_ok(self):
        """Members can call STATUS-class ops."""
        mcp = FakeMcp({"health": "green"})
        res = _ops("member", mcp=mcp).health("skingest")
        assert res.ok
        assert res.outcome == "dispatched"
        assert len(mcp.calls) == 1
        assert mcp.calls[0][0] == "trustee_health"

    def test_logs_agent_ok(self):
        """Agents can call LOG_READ-class ops."""
        mcp = FakeMcp({"lines": ["line1", "line2"]})
        res = _ops("agent", mcp=mcp).logs("skchat-daemon", tail=10)
        assert res.ok
        assert res.outcome == "dispatched"
        assert len(mcp.calls) == 1
        assert mcp.calls[0][1]["tail"] == 10

    def test_deploy_status_guest_denied(self):
        """Guest is denied even for read-only STATUS ops."""
        mcp = FakeMcp()
        res = _ops("guest", mcp=mcp).deploy_status("skchat-daemon")
        assert not res.ok
        assert res.outcome == "rbac_denied"
        assert not res.confirm_required
        # MCP is never called on deny
        assert len(mcp.calls) == 0

    def test_logs_guest_denied(self):
        mcp = FakeMcp()
        res = _ops("guest", mcp=mcp).logs("skchat-daemon")
        assert not res.ok
        assert res.outcome == "rbac_denied"
        assert len(mcp.calls) == 0


# ---------------------------------------------------------------------------
# TRUSTEE-2: restart (DESTRUCTIVE — confirm_required without token)
# ---------------------------------------------------------------------------


class TestRestart:
    def test_restart_operator_no_token_confirm_required(self):
        mcp = FakeMcp()
        res = _ops("operator", mcp=mcp).restart("skchat-daemon")
        assert not res.ok
        assert res.outcome == "confirm_required"
        assert res.confirm_required
        # MCP must NOT be called
        assert len(mcp.calls) == 0

    def test_restart_operator_with_token_dispatched(self):
        mcp = FakeMcp({"restarted": True})
        res = _ops("operator", mcp=mcp).restart("skchat-daemon", confirm_token="tok-abc")
        assert res.ok
        assert res.outcome == "dispatched"
        assert len(mcp.calls) == 1
        assert mcp.calls[0][0] == "trustee_restart"

    def test_restart_owner_no_token_confirm_required(self):
        """Owner also gets confirm_required for destructive — §3.3."""
        mcp = FakeMcp()
        res = _ops("owner", mcp=mcp).restart("skchat-daemon")
        assert res.confirm_required
        assert len(mcp.calls) == 0

    def test_restart_owner_with_token_dispatched(self):
        mcp = FakeMcp({"restarted": True})
        res = _ops("owner", mcp=mcp).restart("skchat-daemon", confirm_token="tok-xyz")
        assert res.ok
        assert res.outcome == "dispatched"

    def test_restart_member_denied(self):
        """Members cannot restart (DESTRUCTIVE class)."""
        mcp = FakeMcp()
        res = _ops("member", mcp=mcp).restart("skchat-daemon", confirm_token="tok")
        # member cannot issue DESTRUCTIVE — deny, not confirm_required
        assert not res.ok
        assert res.outcome == "rbac_denied"
        assert not res.confirm_required
        assert len(mcp.calls) == 0


# ---------------------------------------------------------------------------
# TRUSTEE-3: scale (DEPLOY for scale-up, DESTRUCTIVE for scale-down)
# ---------------------------------------------------------------------------


class TestScale:
    def test_scale_up_operator_no_confirm_needed(self):
        """Scale-up (current=1 → 3) is DEPLOY class — no confirm needed."""
        mcp = FakeMcp({"scaled": True})
        res = _ops("operator", mcp=mcp).scale(
            "skingest", 3, current_replicas=1
        )
        assert res.ok
        assert res.outcome == "dispatched"
        assert len(mcp.calls) == 1
        assert mcp.calls[0][0] == "trustee_scale"

    def test_scale_down_operator_no_token_confirm_required(self):
        """Scale-down (current=3 → 1) is DESTRUCTIVE — confirm required."""
        mcp = FakeMcp()
        res = _ops("operator", mcp=mcp).scale(
            "skingest", 1, current_replicas=3
        )
        assert not res.ok
        assert res.confirm_required
        assert len(mcp.calls) == 0

    def test_scale_down_operator_with_token_dispatched(self):
        mcp = FakeMcp({"scaled": True})
        res = _ops("operator", mcp=mcp).scale(
            "skingest", 1, current_replicas=3, confirm_token="tok-down"
        )
        assert res.ok
        assert res.outcome == "dispatched"

    def test_scale_no_current_replicas_treat_as_deploy(self):
        """When current_replicas not provided, treated as DEPLOY class (safer)."""
        mcp = FakeMcp({"scaled": True})
        res = _ops("operator", mcp=mcp).scale("skingest", 2)
        assert res.ok  # DEPLOY class, operator allowed without confirm

    def test_scale_member_deploy_class_denied(self):
        """Member cannot do DEPLOY-class scale-up."""
        mcp = FakeMcp()
        res = _ops("member", mcp=mcp).scale("skingest", 3, current_replicas=1)
        assert not res.ok
        assert res.outcome == "rbac_denied"
        assert len(mcp.calls) == 0


# ---------------------------------------------------------------------------
# TRUSTEE-4: run_ansible (DEPLOY class)
# ---------------------------------------------------------------------------


class TestRunAnsible:
    def test_operator_dispatched(self):
        mcp = FakeMcp({"rc": 0})
        res = _ops("operator", mcp=mcp).run_ansible(
            "deploy-skchat.yml", extra_vars={"env": "prod"}
        )
        assert res.ok
        assert mcp.calls[0][0] == "run_ansible_playbook"
        assert mcp.calls[0][1]["extra_vars"] == {"env": "prod"}

    def test_member_denied(self):
        mcp = FakeMcp()
        res = _ops("member", mcp=mcp).run_ansible("deploy-skchat.yml")
        assert not res.ok
        assert res.outcome == "rbac_denied"
        assert len(mcp.calls) == 0


# ---------------------------------------------------------------------------
# TRUSTEE-5: Audit records written for every outcome
# ---------------------------------------------------------------------------


class TestAudit:
    def test_audit_written_on_dispatch(self, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        mcp = FakeMcp()
        ops = TrusteeOps(
            issuer_role="operator",
            issuer_fqid="opus@chef.skworld.io",
            mcp_call=mcp,
            audit_writer=AuditWriter(jsonl_path=audit_path),
        )
        ops.deploy_status("skchat-daemon")
        lines = audit_path.read_text().strip().split("\n")
        # At least two records: pre-dispatch + post-dispatch
        assert len(lines) >= 2

    def test_audit_written_on_rbac_deny(self, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        mcp = FakeMcp()
        ops = TrusteeOps(
            issuer_role="guest",
            issuer_fqid="anon@skworld.io",
            mcp_call=mcp,
            audit_writer=AuditWriter(jsonl_path=audit_path),
        )
        ops.deploy_status("skchat-daemon")
        lines = [l for l in audit_path.read_text().strip().split("\n") if l]
        assert len(lines) >= 1

    def test_audit_written_on_confirm_required(self, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        mcp = FakeMcp()
        ops = TrusteeOps(
            issuer_role="operator",
            issuer_fqid="opus@chef.skworld.io",
            mcp_call=mcp,
            audit_writer=AuditWriter(jsonl_path=audit_path),
        )
        ops.restart("skchat-daemon")  # no confirm_token
        lines = [l for l in audit_path.read_text().strip().split("\n") if l]
        assert len(lines) >= 1


# ---------------------------------------------------------------------------
# TRUSTEE-6: MCP callable not called on deny
# ---------------------------------------------------------------------------


class TestMcpNotCalledOnDeny:
    @pytest.mark.parametrize("role", ["guest", "member"])
    def test_no_mcp_call_on_rbac_deny(self, role: str):
        mcp = FakeMcp()
        _ops(role, mcp=mcp).restart("skchat-daemon", confirm_token="tok")
        # member → denied (DESTRUCTIVE); guest → denied (STATUS also denied)
        assert len(mcp.calls) == 0


# ---------------------------------------------------------------------------
# TRUSTEE-7: MCP callable exception → error outcome
# ---------------------------------------------------------------------------


class TestMcpException:
    def test_mcp_exception_returns_error(self):
        exc = RuntimeError("MCP server timeout")
        mcp = FakeMcp(raise_exc=exc)
        res = _ops("operator", mcp=mcp).deploy_status("skchat-daemon")
        assert not res.ok
        assert res.outcome == "error"
        assert "MCP server timeout" in res.error


# ---------------------------------------------------------------------------
# Coord / ITIL read ops
# ---------------------------------------------------------------------------


class TestCoordItil:
    def test_coord_status_operator(self):
        mcp = FakeMcp({"tasks": []})
        res = _ops("operator", mcp=mcp).coord_status()
        assert res.ok
        assert mcp.calls[0][0] == "coord_status"

    def test_itil_incident_list_operator(self):
        mcp = FakeMcp({"incidents": []})
        res = _ops("operator", mcp=mcp).itil_incident_list()
        assert res.ok
        assert mcp.calls[0][0] == "itil_incident_list"

    def test_coord_status_guest_denied(self):
        mcp = FakeMcp()
        res = _ops("guest", mcp=mcp).coord_status("abc123")
        assert not res.ok
        assert res.outcome == "rbac_denied"
