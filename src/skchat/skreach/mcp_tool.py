"""skreach MCP tool surface — F4 agent-drives-agent / agent-drives-node.

Exposes skreach operations as OpenAI-style tool schemas + async handlers,
compatible with the voice_engine Tool/ToolRegistry pattern
(`voice_engine/tools.py`).

Tools defined here:
  skreach_status     — deploy + health status for a service        [STATUS]
  skreach_logs       — recent log lines for a service              [LOG_READ]
  skreach_deploy     — scale/restart a service (via TrusteeOps)    [DEPLOY/DESTRUCTIVE]
  skreach_exec       — interactive signed exec (always gated)      [EXEC]

RBAC tiers:
  skreach_status   — operator + member + agent (read-only)
  skreach_logs     — operator + member + agent (read-only)
  skreach_deploy   — operator only (destructive → confirm_required by default)
  skreach_exec     — operator/owner only; ALWAYS returns ExecDisabled or
                     confirm_required — never actually executes in this pass

All tools are operator_only=True in the voice_engine sense (is_operator gate
applies); skreach_exec additionally requires owner or operator AND a
confirm_token to proceed past confirm_required.

The handlers receive a `ctx` dict that MUST contain:
  ctx["issuer_role"]  — resolved role string (owner/operator/member/…)
  ctx["issuer_fqid"]  — issuer FQID string
  ctx["mcp_call"]     — optional McpCallable (falls back to TrusteeOps default)
  ctx["audit_writer"] — optional AuditWriter instance
  ctx["node_fqid"]    — optional str

Spec: docs/superpowers/specs/2026-06-12-skchat-architecture-reassessment.md §2.6
      docs/superpowers/specs/2026-06-13-skreach-security.md §2–§4
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .audit import AuditWriter
from .rbac import CommandClass, authorize
from .sandbox import ExecDisabled
from .trustee import TrusteeOps

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool dataclass (mirrors voice_engine/tools.py Tool for registry compatibility)
# ---------------------------------------------------------------------------

Handler = Callable[[dict, dict], Awaitable[str]]


@dataclass
class Tool:
    """skreach MCP tool descriptor — registry-compatible with voice_engine Tool.

    Attributes:
        name:          Tool name (as seen by the LLM / MCP client).
        schema:        OpenAI function schema dict.
        handler:       Async (args, ctx) -> str handler.
        operator_only: If True, requires is_operator=True in the registry
                       dispatch (same gate as voice_engine).
        rbac_class:    The CommandClass used for RBAC evaluation inside the
                       handler (informational; enforcement is in the handler).
    """

    name: str
    schema: dict
    handler: Handler | None = None
    operator_only: bool = True  # all skreach tools require at least operator
    rbac_class: str = CommandClass.STATUS


@dataclass
class ToolRegistry:
    """skreach tool registry — compatible with voice_engine ToolRegistry API."""

    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def openai_schemas(self) -> list[dict]:
        return [t.schema for t in self._tools.values()]

    async def dispatch(
        self,
        name: str,
        args: dict,
        *,
        is_operator: bool = False,
        ctx: dict | None = None,
    ) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"unknown skreach tool: {name}"
        if not is_operator:
            return f"PERMISSION DENIED: '{name}' requires operator or higher"
        if tool.handler is None:
            return f"skreach tool '{name}' has no handler registered"
        try:
            return await tool.handler(args, ctx or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("skreach tool %s failed: %r", name, exc)
            return f"{name} failed: {exc}"


# ---------------------------------------------------------------------------
# Handler helpers
# ---------------------------------------------------------------------------


def _ops_from_ctx(ctx: dict) -> TrusteeOps:
    """Build a TrusteeOps from the handler context dict."""
    return TrusteeOps(
        issuer_role=ctx.get("issuer_role", "guest"),
        issuer_fqid=ctx.get("issuer_fqid", ""),
        mcp_call=ctx.get("mcp_call"),  # None → TrusteeOps uses its stub
        audit_writer=ctx.get("audit_writer"),  # None → TrusteeOps creates default
        node_fqid=ctx.get("node_fqid", ""),
    )


# ---------------------------------------------------------------------------
# Tool: skreach_status
# ---------------------------------------------------------------------------

_STATUS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "skreach_status",
        "description": (
            "Query deploy and health status for a SKWorld service. "
            "Returns deploy_status + trustee_health output. "
            "Operator, member, and agent roles may call this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name (e.g. 'skchat-daemon', 'skingest').",
                }
            },
            "required": ["service"],
        },
    },
}


async def _handle_skreach_status(args: dict, ctx: dict) -> str:
    service = args.get("service", "")
    if not service:
        return "error: 'service' is required"
    ops = _ops_from_ctx(ctx)
    status_res = ops.deploy_status(service)
    health_res = ops.health(service)
    if not status_res.ok and not health_res.ok:
        return (
            f"skreach_status denied: {status_res.deny_reason or health_res.deny_reason}"
        )
    return (
        f"deploy_status({service}): {status_res.outcome} data={status_res.data} | "
        f"health({service}): {health_res.outcome} data={health_res.data}"
    )


# ---------------------------------------------------------------------------
# Tool: skreach_logs
# ---------------------------------------------------------------------------

_LOGS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "skreach_logs",
        "description": (
            "Fetch recent log lines for a SKWorld service via trustee_logs. "
            "Operator, member, and agent roles may call this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name.",
                },
                "tail": {
                    "type": "integer",
                    "description": "Number of log lines to return (default 50).",
                    "default": 50,
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Optional ISO-8601 timestamp; only return logs after this time."
                    ),
                },
            },
            "required": ["service"],
        },
    },
}


async def _handle_skreach_logs(args: dict, ctx: dict) -> str:
    service = args.get("service", "")
    if not service:
        return "error: 'service' is required"
    tail = int(args.get("tail", 50))
    since = args.get("since")
    ops = _ops_from_ctx(ctx)
    res = ops.logs(service, tail=tail, since=since)
    if not res.ok:
        if res.confirm_required:
            return f"confirm_required: {res.deny_reason}"
        return f"skreach_logs denied: {res.deny_reason or res.error}"
    return f"logs({service} tail={tail}): {res.outcome} data={res.data}"


# ---------------------------------------------------------------------------
# Tool: skreach_deploy
# ---------------------------------------------------------------------------

_DEPLOY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "skreach_deploy",
        "description": (
            "Restart or scale a SKWorld service via TrusteeOps. "
            "DESTRUCTIVE operations (restart, scale-down) require a confirm_token. "
            "Operator role required; member and agent are denied."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["restart", "scale"],
                    "description": "'restart' or 'scale'.",
                },
                "service": {
                    "type": "string",
                    "description": "Service name.",
                },
                "replicas": {
                    "type": "integer",
                    "description": "Desired replica count (required for scale action).",
                },
                "current_replicas": {
                    "type": "integer",
                    "description": (
                        "Current replica count. When provided and replicas < "
                        "current_replicas, the op is treated as scale-down (destructive)."
                    ),
                },
                "confirm_token": {
                    "type": "string",
                    "description": (
                        "Confirm token required for destructive ops "
                        "(restart, scale-down). Omit to get confirm_required."
                    ),
                },
            },
            "required": ["action", "service"],
        },
    },
}


async def _handle_skreach_deploy(args: dict, ctx: dict) -> str:
    action = args.get("action", "")
    service = args.get("service", "")
    if not action or not service:
        return "error: 'action' and 'service' are required"
    confirm_token: str | None = args.get("confirm_token") or None
    ops = _ops_from_ctx(ctx)

    if action == "restart":
        res = ops.restart(service, confirm_token=confirm_token)
    elif action == "scale":
        replicas = args.get("replicas")
        if replicas is None:
            return "error: 'replicas' is required for scale action"
        current = args.get("current_replicas")
        res = ops.scale(
            service,
            int(replicas),
            current_replicas=int(current) if current is not None else None,
            confirm_token=confirm_token,
        )
    else:
        return f"error: unknown action '{action}' (must be restart or scale)"

    if res.confirm_required:
        return (
            f"confirm_required: {res.deny_reason} — "
            "re-call with confirm_token to proceed"
        )
    if not res.ok:
        return f"skreach_deploy denied: {res.deny_reason or res.error}"
    return f"deploy({action} {service}): {res.outcome} data={res.data}"


# ---------------------------------------------------------------------------
# Tool: skreach_exec
# ---------------------------------------------------------------------------

_EXEC_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "skreach_exec",
        "description": (
            "Interactive signed exec on a skreach node. "
            "Routes through the signed-envelope model (RBAC + exec-gate). "
            "Returns ExecDisabled or confirm_required in the current F4 pass — "
            "NEVER actually executes. Owner or operator role required. "
            "A confirm_token is still required for execution even when the gate "
            "is enabled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_fqid": {
                    "type": "string",
                    "description": "Target node FQID.",
                },
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Command + arguments as a list (no shell interpolation)."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory on the target node.",
                },
                "confirm_token": {
                    "type": "string",
                    "description": "Confirm token required for exec (destructive).",
                },
            },
            "required": ["node_fqid", "argv"],
        },
    },
}


async def _handle_skreach_exec(args: dict, ctx: dict) -> str:
    """skreach_exec handler.

    Always evaluates RBAC (exec class) and the exec gate.  Returns either
    'rbac_denied', 'confirm_required', or 'exec_disabled' — never actually
    executes in this pass (SKREACH_EXEC_ENABLED must be "1" AND a
    confirm_token must be present to reach the exec layer).
    """
    issuer_role: str = ctx.get("issuer_role", "guest")
    issuer_fqid: str = ctx.get("issuer_fqid", "")
    confirm_token: str | None = args.get("confirm_token") or None
    argv: list[str] = args.get("argv") or []
    node_fqid: str = args.get("node_fqid", "")

    if not argv:
        return "error: 'argv' is required and must be non-empty"

    # Step 1: RBAC check — exec class
    decision = authorize(
        role=issuer_role,
        cmd_class=CommandClass.EXEC,
        issuer_fqid=issuer_fqid,
    )

    audit_writer: AuditWriter = ctx.get("audit_writer") or AuditWriter()

    if decision.denied:
        audit_writer.write_rejection(
            outcome="rbac_denied",
            iss_fqid=issuer_fqid,
            role=issuer_role,
            cmd_class=CommandClass.EXEC,
            op="exec",
            argv=argv,
        )
        return f"RBAC denied: {decision.reason}"

    # Step 2: Confirm gate — exec is always destructive, confirm required
    if decision.confirm_required or not confirm_token:
        audit_writer.write_rejection(
            outcome="confirm_required",
            iss_fqid=issuer_fqid,
            role=issuer_role,
            cmd_class=CommandClass.EXEC,
            op="exec",
            argv=argv,
        )
        return (
            "confirm_required: exec is a destructive class; "
            "re-call with a confirm_token to proceed past this gate. "
            "NOTE: exec is gated off in the current deployment "
            f"(SKREACH_EXEC_ENABLED={os.environ.get('SKREACH_EXEC_ENABLED', '0')})"
        )

    # Step 3: Exec gate — even if RBAC + confirm pass, exec may be disabled
    exec_enabled = os.environ.get("SKREACH_EXEC_ENABLED", "0").strip() == "1"
    if not exec_enabled:
        audit_writer.write_rejection(
            outcome="exec_disabled",
            iss_fqid=issuer_fqid,
            role=issuer_role,
            cmd_class=CommandClass.EXEC,
            op="exec",
            argv=argv,
        )
        result = ExecDisabled(
            argv=argv,
            cwd=args.get("cwd", ""),
            scrubbed_keys=[],
        )
        return (
            f"exec_disabled: SKREACH_EXEC_ENABLED is not set; "
            f"node={node_fqid} argv={result.argv!r} cwd={result.cwd!r}"
        )

    # If we get here (exec enabled + confirm present), return a stub.
    # Real dispatch to the skreachd transport is wired in F5 (terminal lane).
    return (
        f"exec_stub: RBAC passed, confirm present, exec gate open — "
        f"transport dispatch not yet wired (F5). node={node_fqid} argv={argv!r}"
    )


# ---------------------------------------------------------------------------
# Registry factory
# ---------------------------------------------------------------------------


def build_registry() -> ToolRegistry:
    """Build and return the default skreach MCP tool registry.

    Registers all four F4 tools.  Callers can register additional tools or
    replace individual handlers after construction.
    """
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="skreach_status",
            schema=_STATUS_SCHEMA,
            handler=_handle_skreach_status,
            operator_only=True,
            rbac_class=CommandClass.STATUS,
        )
    )
    registry.register(
        Tool(
            name="skreach_logs",
            schema=_LOGS_SCHEMA,
            handler=_handle_skreach_logs,
            operator_only=True,
            rbac_class=CommandClass.LOG_READ,
        )
    )
    registry.register(
        Tool(
            name="skreach_deploy",
            schema=_DEPLOY_SCHEMA,
            handler=_handle_skreach_deploy,
            operator_only=True,
            rbac_class=CommandClass.DEPLOY,
        )
    )
    registry.register(
        Tool(
            name="skreach_exec",
            schema=_EXEC_SCHEMA,
            handler=_handle_skreach_exec,
            operator_only=True,
            rbac_class=CommandClass.EXEC,
        )
    )

    return registry


# Module-level default registry (importable directly)
default_registry: ToolRegistry = build_registry()
