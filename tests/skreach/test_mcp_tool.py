"""Tests for skchat.skreach.mcp_tool — skreach MCP tool surface (F4).

All tests are fully offline — no real skcapstone MCP, no network, no exec.

Covers:
  MCP-1: skreach_status dispatches to TrusteeOps.deploy_status + .health.
  MCP-2: skreach_logs dispatches to TrusteeOps.logs.
  MCP-3: skreach_deploy routes to .restart or .scale with correct destructive gating.
  MCP-4: skreach_exec always returns gated/confirm_required (never actually execs).
  MCP-5: ToolRegistry denies non-operator (is_operator=False) for all tools.
  MCP-6: ToolRegistry.openai_schemas() returns all four tool schemas.
  MCP-7: Unknown tool name returns a clear error string.
  MCP-8: build_registry() produces the correct four tools.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from skchat.skreach.audit import AuditWriter
from skchat.skreach.mcp_tool import (
    Tool,
    ToolRegistry,
    _handle_skreach_deploy,
    _handle_skreach_exec,
    _handle_skreach_logs,
    _handle_skreach_status,
    build_registry,
    default_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeMcp:
    def __init__(self, response: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._response = response or {"status": "ok"}

    def __call__(self, tool_name: str, **kwargs: Any) -> dict:
        self.calls.append((tool_name, kwargs))
        return self._response


def _ctx(
    role: str = "operator",
    fqid: str = "opus@chef.skworld.io",
    mcp: FakeMcp | None = None,
    audit_path: Path | None = None,
) -> dict:
    writer = AuditWriter(jsonl_path=audit_path or Path("/dev/null"))
    return {
        "issuer_role": role,
        "issuer_fqid": fqid,
        "mcp_call": mcp,
        "audit_writer": writer,
        "node_fqid": "noroc2027@chef.skworld.io",
    }


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# MCP-1: skreach_status
# ---------------------------------------------------------------------------


class TestSkreachStatus:
    def test_operator_dispatched(self):
        mcp = FakeMcp({"deploy": "running"})
        result = run(_handle_skreach_status({"service": "skchat-daemon"}, _ctx(mcp=mcp)))
        assert "dispatched" in result
        assert "skchat-daemon" in result
        # Two MCP calls: deploy_status + trustee_health
        assert len(mcp.calls) == 2
        tool_names = {c[0] for c in mcp.calls}
        assert "deploy_status" in tool_names
        assert "trustee_health" in tool_names

    def test_guest_denied(self):
        mcp = FakeMcp()
        result = run(
            _handle_skreach_status({"service": "skchat-daemon"}, _ctx(role="guest", mcp=mcp))
        )
        assert "denied" in result.lower()
        assert len(mcp.calls) == 0

    def test_missing_service(self):
        result = run(_handle_skreach_status({}, _ctx()))
        assert "required" in result.lower()

    def test_member_can_read_status(self):
        """Member has STATUS access."""
        mcp = FakeMcp()
        result = run(
            _handle_skreach_status({"service": "skingest"}, _ctx(role="member", mcp=mcp))
        )
        assert "dispatched" in result


# ---------------------------------------------------------------------------
# MCP-2: skreach_logs
# ---------------------------------------------------------------------------


class TestSkreachLogs:
    def test_operator_dispatched_with_tail(self):
        mcp = FakeMcp({"lines": []})
        result = run(
            _handle_skreach_logs(
                {"service": "skchat-daemon", "tail": 20}, _ctx(mcp=mcp)
            )
        )
        assert "dispatched" in result
        assert len(mcp.calls) == 1
        assert mcp.calls[0][1]["tail"] == 20

    def test_agent_can_read_logs(self):
        """Agents have LOG_READ access."""
        mcp = FakeMcp()
        result = run(
            _handle_skreach_logs(
                {"service": "skchat-daemon"}, _ctx(role="agent", mcp=mcp)
            )
        )
        assert "dispatched" in result

    def test_guest_denied(self):
        mcp = FakeMcp()
        result = run(
            _handle_skreach_logs(
                {"service": "skchat-daemon"}, _ctx(role="guest", mcp=mcp)
            )
        )
        assert "denied" in result.lower()
        assert len(mcp.calls) == 0

    def test_missing_service(self):
        result = run(_handle_skreach_logs({}, _ctx()))
        assert "required" in result.lower()

    def test_since_kwarg_forwarded(self):
        mcp = FakeMcp()
        run(
            _handle_skreach_logs(
                {"service": "skchat-daemon", "since": "2026-06-13T00:00:00Z"},
                _ctx(mcp=mcp),
            )
        )
        assert mcp.calls[0][1]["since"] == "2026-06-13T00:00:00Z"


# ---------------------------------------------------------------------------
# MCP-3: skreach_deploy
# ---------------------------------------------------------------------------


class TestSkreachDeploy:
    def test_restart_no_token_confirm_required(self):
        mcp = FakeMcp()
        result = run(
            _handle_skreach_deploy(
                {"action": "restart", "service": "skchat-daemon"}, _ctx(mcp=mcp)
            )
        )
        assert "confirm_required" in result
        assert len(mcp.calls) == 0

    def test_restart_with_token_dispatched(self):
        mcp = FakeMcp({"restarted": True})
        result = run(
            _handle_skreach_deploy(
                {
                    "action": "restart",
                    "service": "skchat-daemon",
                    "confirm_token": "tok-restart",
                },
                _ctx(mcp=mcp),
            )
        )
        assert "dispatched" in result
        assert len(mcp.calls) == 1
        assert mcp.calls[0][0] == "trustee_restart"

    def test_scale_up_no_confirm_needed(self):
        """Scale-up is DEPLOY class — no confirm needed for operator."""
        mcp = FakeMcp({"scaled": True})
        result = run(
            _handle_skreach_deploy(
                {
                    "action": "scale",
                    "service": "skingest",
                    "replicas": 3,
                    "current_replicas": 1,
                },
                _ctx(mcp=mcp),
            )
        )
        assert "dispatched" in result
        assert len(mcp.calls) == 1
        assert mcp.calls[0][0] == "trustee_scale"

    def test_scale_down_no_token_confirm_required(self):
        mcp = FakeMcp()
        result = run(
            _handle_skreach_deploy(
                {
                    "action": "scale",
                    "service": "skingest",
                    "replicas": 1,
                    "current_replicas": 3,
                },
                _ctx(mcp=mcp),
            )
        )
        assert "confirm_required" in result
        assert len(mcp.calls) == 0

    def test_scale_down_with_token_dispatched(self):
        mcp = FakeMcp({"scaled": True})
        result = run(
            _handle_skreach_deploy(
                {
                    "action": "scale",
                    "service": "skingest",
                    "replicas": 1,
                    "current_replicas": 3,
                    "confirm_token": "tok-down",
                },
                _ctx(mcp=mcp),
            )
        )
        assert "dispatched" in result

    def test_member_restart_denied(self):
        mcp = FakeMcp()
        result = run(
            _handle_skreach_deploy(
                {
                    "action": "restart",
                    "service": "skchat-daemon",
                    "confirm_token": "tok",
                },
                _ctx(role="member", mcp=mcp),
            )
        )
        assert "denied" in result.lower()
        assert len(mcp.calls) == 0

    def test_unknown_action_error(self):
        result = run(
            _handle_skreach_deploy(
                {"action": "nuke", "service": "skchat-daemon"}, _ctx()
            )
        )
        assert "unknown action" in result.lower()

    def test_scale_missing_replicas_error(self):
        result = run(
            _handle_skreach_deploy({"action": "scale", "service": "skingest"}, _ctx())
        )
        assert "replicas" in result.lower() and "required" in result.lower()


# ---------------------------------------------------------------------------
# MCP-4: skreach_exec — always gated / confirm_required / exec_disabled
# ---------------------------------------------------------------------------


class TestSkreachExec:
    def test_exec_guest_rbac_denied(self):
        result = run(
            _handle_skreach_exec(
                {"node_fqid": "noroc2027", "argv": ["ls", "-la"]},
                _ctx(role="guest"),
            )
        )
        assert "rbac" in result.lower() and "denied" in result.lower()

    def test_exec_member_rbac_denied(self):
        result = run(
            _handle_skreach_exec(
                {"node_fqid": "noroc2027", "argv": ["ls", "-la"]},
                _ctx(role="member"),
            )
        )
        assert "denied" in result.lower()

    def test_exec_agent_rbac_denied_no_grant(self):
        """Agent without per-node exec grant → denied."""
        result = run(
            _handle_skreach_exec(
                {"node_fqid": "noroc2027", "argv": ["skcapstone", "status"]},
                _ctx(role="agent"),
            )
        )
        assert "denied" in result.lower()

    def test_exec_operator_no_confirm_returns_confirm_required(self):
        """Operator without confirm_token → confirm_required (not denied)."""
        result = run(
            _handle_skreach_exec(
                {"node_fqid": "noroc2027", "argv": ["skcapstone", "status"]},
                _ctx(role="operator"),
            )
        )
        assert "confirm_required" in result

    def test_exec_operator_with_confirm_exec_disabled(self, monkeypatch):
        """Even with confirm_token, exec gate (SKREACH_EXEC_ENABLED=0) blocks exec."""
        monkeypatch.setenv("SKREACH_EXEC_ENABLED", "0")
        result = run(
            _handle_skreach_exec(
                {
                    "node_fqid": "noroc2027",
                    "argv": ["skcapstone", "status"],
                    "confirm_token": "tok-exec",
                },
                _ctx(role="operator"),
            )
        )
        assert "exec_disabled" in result

    def test_exec_owner_with_confirm_exec_disabled(self, monkeypatch):
        """Owner + confirm still hits exec_disabled gate."""
        monkeypatch.setenv("SKREACH_EXEC_ENABLED", "0")
        result = run(
            _handle_skreach_exec(
                {
                    "node_fqid": "noroc2027",
                    "argv": ["skcapstone", "status"],
                    "confirm_token": "tok-owner",
                },
                _ctx(role="owner"),
            )
        )
        assert "exec_disabled" in result

    def test_exec_empty_argv_error(self):
        result = run(
            _handle_skreach_exec(
                {"node_fqid": "noroc2027", "argv": []},
                _ctx(role="operator"),
            )
        )
        assert "required" in result.lower()

    def test_exec_missing_argv_error(self):
        result = run(
            _handle_skreach_exec(
                {"node_fqid": "noroc2027"},
                _ctx(role="operator"),
            )
        )
        assert "required" in result.lower()

    def test_exec_popen_not_called(self, monkeypatch):
        """Direct version: Popen must not be called."""
        import subprocess
        monkeypatch.setenv("SKREACH_EXEC_ENABLED", "0")
        popen_calls = []

        class _SentinelPopen:
            def __init__(self, *a, **kw):
                popen_calls.append((a, kw))

        monkeypatch.setattr(subprocess, "Popen", _SentinelPopen)
        run(
            _handle_skreach_exec(
                {
                    "node_fqid": "noroc2027",
                    "argv": ["ls"],
                    "confirm_token": "tok",
                },
                _ctx(role="operator"),
            )
        )
        assert len(popen_calls) == 0, "subprocess.Popen was called — exec gate is broken"


# ---------------------------------------------------------------------------
# MCP-5: ToolRegistry.dispatch — non-operator always denied
# ---------------------------------------------------------------------------


class TestRegistryDispatch:
    def test_non_operator_denied_for_all_tools(self):
        registry = build_registry()
        for tool_name in ("skreach_status", "skreach_logs", "skreach_deploy", "skreach_exec"):
            result = run(
                registry.dispatch(
                    tool_name,
                    {"service": "skchat-daemon", "argv": ["ls"]},
                    is_operator=False,
                    ctx=_ctx(),
                )
            )
            assert "PERMISSION DENIED" in result, (
                f"Expected PERMISSION DENIED for {tool_name!r} but got {result!r}"
            )

    def test_operator_status_dispatched(self):
        mcp = FakeMcp()
        registry = build_registry()
        result = run(
            registry.dispatch(
                "skreach_status",
                {"service": "skchat-daemon"},
                is_operator=True,
                ctx=_ctx(mcp=mcp),
            )
        )
        # Should reach handler; guest would deny via RBAC inside handler
        # Here issuer_role=operator so should be dispatched
        assert "dispatched" in result

    def test_unknown_tool_name(self):
        registry = build_registry()
        result = run(
            registry.dispatch("skreach_nonexistent", {}, is_operator=True, ctx=_ctx())
        )
        assert "unknown" in result.lower()


# ---------------------------------------------------------------------------
# MCP-6: openai_schemas() returns all four tool schemas
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_four_schemas_registered(self):
        registry = build_registry()
        schemas = registry.openai_schemas()
        assert len(schemas) == 4

    def test_schema_names(self):
        registry = build_registry()
        names = {s["function"]["name"] for s in registry.openai_schemas()}
        assert names == {
            "skreach_status",
            "skreach_logs",
            "skreach_deploy",
            "skreach_exec",
        }

    def test_schema_format(self):
        """Each schema must have type=function and a parameters dict."""
        registry = build_registry()
        for schema in registry.openai_schemas():
            assert schema["type"] == "function"
            assert "function" in schema
            assert "parameters" in schema["function"]
            assert "description" in schema["function"]


# ---------------------------------------------------------------------------
# MCP-7: build_registry tools have correct rbac_class annotations
# ---------------------------------------------------------------------------


class TestRbacAnnotations:
    def test_status_rbac_class(self):
        registry = build_registry()
        tool = registry._tools["skreach_status"]
        assert tool.rbac_class == "status"

    def test_logs_rbac_class(self):
        registry = build_registry()
        tool = registry._tools["skreach_logs"]
        assert tool.rbac_class == "log_read"

    def test_deploy_rbac_class(self):
        registry = build_registry()
        tool = registry._tools["skreach_deploy"]
        assert tool.rbac_class == "deploy"

    def test_exec_rbac_class(self):
        registry = build_registry()
        tool = registry._tools["skreach_exec"]
        assert tool.rbac_class == "exec"

    def test_all_tools_operator_only(self):
        registry = build_registry()
        for tool in registry._tools.values():
            assert tool.operator_only is True, (
                f"Tool {tool.name!r} must have operator_only=True"
            )


# ---------------------------------------------------------------------------
# MCP-8: default_registry is usable
# ---------------------------------------------------------------------------


class TestDefaultRegistry:
    def test_default_registry_has_four_tools(self):
        assert len(default_registry.openai_schemas()) == 4

    def test_default_registry_exec_gated(self, monkeypatch):
        monkeypatch.setenv("SKREACH_EXEC_ENABLED", "0")
        result = run(
            default_registry.dispatch(
                "skreach_exec",
                {
                    "node_fqid": "noroc2027",
                    "argv": ["ls"],
                    "confirm_token": "tok",
                },
                is_operator=True,
                ctx=_ctx(role="operator"),
            )
        )
        assert "exec_disabled" in result or "confirm_required" in result
