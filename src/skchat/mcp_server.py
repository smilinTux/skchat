"""SKChat MCP Server — chat tools for AI agents.

Exposes SKChat's messaging and group management as MCP tools so any
AI agent (Cursor, Claude Code, Claude Desktop, Windsurf, Cline...)
can send messages, check inbox, create groups, and broadcast to teams
via tool calls.

Tools:
    send_message       — Send a message to a recipient
    check_inbox        — Retrieve incoming messages
    search_messages    — Full-text search across message history
    create_group       — Create a new group chat
    group_send         — Send a message to a group
    group_members      — List members of a group
    group_add_member   — Add a member to a group
    list_threads       — List conversation threads
    get_thread         — Get messages in a thread

Invocation:
    python -m skchat.mcp_server

Client configuration (Cursor / Claude Desktop / Claude Code CLI):
    {"mcpServers": {"skchat": {
        "command": "python", "args": ["-m", "skchat.mcp_server"]}}}
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .agent_comm import AgentMessenger
from .group import GroupChat, GroupMember, MemberRole, ParticipantType
from .history import ChatHistory
from .identity_bridge import get_sovereign_identity
from .models import ChatMessage, ContentType, DeliveryStatus

logger = logging.getLogger("skchat.mcp")

# Module-level singletons — lazy init on first tool call.
_identity: Optional[str] = None
_history: Optional[ChatHistory] = None
_messenger: Optional[AgentMessenger] = None
_groups: dict[str, GroupChat] = {}  # In-memory group registry

server = Server("skchat")


# ─────────────────────────────────────────────────────────────
# Lazy initialization
# ─────────────────────────────────────────────────────────────


def _get_identity() -> str:
    """Get or resolve the sovereign identity."""
    global _identity
    if _identity is None:
        try:
            _identity = get_sovereign_identity()
        except Exception:
            _identity = "capauth:agent@skchat.local"
    return _identity


def _get_history() -> ChatHistory:
    """Get or initialize the ChatHistory."""
    global _history
    if _history is None:
        _history = ChatHistory.from_config()
    return _history


def _get_messenger() -> AgentMessenger:
    """Get or initialize the AgentMessenger."""
    global _messenger
    if _messenger is None:
        _messenger = AgentMessenger(
            identity=_get_identity(),
            history=_get_history(),
        )
    return _messenger


# ─────────────────────────────────────────────────────────────
# Response helpers
# ─────────────────────────────────────────────────────────────


def _json(data: Any) -> list[TextContent]:
    """Wrap data as a JSON TextContent response."""
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _error(message: str) -> list[TextContent]:
    """Return an error payload."""
    return [TextContent(type="text", text=json.dumps({"error": message}))]


# ─────────────────────────────────────────────────────────────
# Tool Definitions
# ─────────────────────────────────────────────────────────────


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Register all SKChat tools with the MCP server."""
    return [
        Tool(
            name="send_message",
            description=(
                "Send a chat message to a recipient. Messages are stored in "
                "sovereign history and optionally delivered via SKComm P2P transport."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": (
                            "Recipient identity URI (e.g. 'capauth:lumina@skworld.io') "
                            "or short name (e.g. 'lumina')."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Message content (markdown supported).",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to reply in (optional).",
                    },
                    "reply_to": {
                        "type": "string",
                        "description": "Message ID this is a reply to (optional).",
                    },
                    "message_type": {
                        "type": "string",
                        "enum": ["text", "finding", "task", "query", "response"],
                        "description": "Structured message type (default: text).",
                    },
                },
                "required": ["recipient", "content"],
            },
        ),
        Tool(
            name="check_inbox",
            description=(
                "Check for incoming messages. Returns recent messages from "
                "the agent's inbox, optionally filtered by message type."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum messages to return (default: 20).",
                    },
                    "message_type": {
                        "type": "string",
                        "enum": ["text", "finding", "task", "query", "response"],
                        "description": "Filter by message type (optional).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="search_messages",
            description=(
                "Full-text search across message history. "
                "Returns matching messages ranked by relevance."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default: 20).",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="create_group",
            description=(
                "Create a new group chat with specified members. "
                "The creating agent becomes admin. Supports human and AI members."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Group name.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Group description (optional).",
                    },
                    "members": {
                        "type": "array",
                        "description": "Members to add (identity URIs or short names).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "identity": {
                                    "type": "string",
                                    "description": "Member identity URI or short name.",
                                },
                                "role": {
                                    "type": "string",
                                    "enum": ["admin", "member", "observer"],
                                    "description": "Member role (default: member).",
                                },
                                "participant_type": {
                                    "type": "string",
                                    "enum": ["human", "agent", "service"],
                                    "description": "Type of participant (default: agent).",
                                },
                            },
                            "required": ["identity"],
                        },
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="group_send",
            description=(
                "Send a message to a group chat. The message is delivered "
                "to all group members."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "Group ID to send to.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Message content.",
                    },
                },
                "required": ["group_id", "content"],
            },
        ),
        Tool(
            name="group_members",
            description=(
                "List all members of a group chat with their roles "
                "and participant types."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "Group ID.",
                    },
                },
                "required": ["group_id"],
            },
        ),
        Tool(
            name="group_add_member",
            description=(
                "Add a new member to an existing group chat. "
                "Requires admin privileges."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "Group ID.",
                    },
                    "identity": {
                        "type": "string",
                        "description": "New member's identity URI or short name.",
                    },
                    "role": {
                        "type": "string",
                        "enum": ["admin", "member", "observer"],
                        "description": "Role (default: member).",
                    },
                    "participant_type": {
                        "type": "string",
                        "enum": ["human", "agent", "service"],
                        "description": "Type (default: agent).",
                    },
                },
                "required": ["group_id", "identity"],
            },
        ),
        Tool(
            name="list_threads",
            description=(
                "List conversation threads. Returns thread IDs, titles, "
                "participant counts, and activity timestamps."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum threads to return (default: 20).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_thread",
            description=(
                "Get messages from a specific conversation thread. "
                "Returns messages in chronological order."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum messages (default: 50).",
                    },
                },
                "required": ["thread_id"],
            },
        ),
    ]


# ─────────────────────────────────────────────────────────────
# Tool Dispatch
# ─────────────────────────────────────────────────────────────


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch incoming tool calls to the appropriate handler."""
    handlers = {
        "send_message": _handle_send_message,
        "check_inbox": _handle_check_inbox,
        "search_messages": _handle_search_messages,
        "create_group": _handle_create_group,
        "group_send": _handle_group_send,
        "group_members": _handle_group_members,
        "group_add_member": _handle_group_add_member,
        "list_threads": _handle_list_threads,
        "get_thread": _handle_get_thread,
    }
    handler = handlers.get(name)
    if handler is None:
        return _error(f"Unknown tool: {name}")
    try:
        return await handler(arguments)
    except Exception as exc:
        logger.exception("Tool '%s' failed", name)
        return _error(f"{name} failed: {exc}")


# ─────────────────────────────────────────────────────────────
# Tool Handlers — Messaging
# ─────────────────────────────────────────────────────────────


async def _handle_send_message(args: dict) -> list[TextContent]:
    """Send a message to a recipient.

    Args:
        args: recipient, content, optional thread_id, reply_to, message_type.

    Returns:
        JSON with delivery result.
    """
    recipient: str = args.get("recipient", "")
    content: str = args.get("content", "")

    if not recipient:
        return _error("recipient is required")
    if not content:
        return _error("content is required")

    messenger = _get_messenger()
    message_type: str = args.get("message_type", "text")
    thread_id: str | None = args.get("thread_id")
    reply_to: str | None = args.get("reply_to")

    # Resolve short names to full URIs
    if not recipient.startswith("capauth:"):
        try:
            from .identity_bridge import resolve_peer_name
            recipient = resolve_peer_name(recipient)
        except Exception:
            # Use as-is if resolution fails
            pass

    result = messenger.send(
        recipient=recipient,
        content=content,
        message_type=message_type,
        thread_id=thread_id,
        reply_to=reply_to,
    )

    return _json({
        "sent": True,
        "message_id": result.get("message_id"),
        "recipient": recipient,
        "thread_id": thread_id,
        "delivered": result.get("delivered", False),
        "transport": result.get("transport", "local"),
    })


async def _handle_check_inbox(args: dict) -> list[TextContent]:
    """Check for incoming messages.

    Args:
        args: Optional limit, message_type.

    Returns:
        JSON list of messages.
    """
    messenger = _get_messenger()
    limit: int = args.get("limit", 20)
    message_type: str | None = args.get("message_type")

    messages = messenger.get_inbox(limit=limit, message_type=message_type)

    return _json({
        "count": len(messages),
        "messages": [
            {
                "id": m.get("id", m.get("message_id", "")),
                "sender": m.get("sender", ""),
                "content": m.get("content", "")[:500],
                "timestamp": m.get("timestamp", ""),
                "thread_id": m.get("thread_id"),
                "message_type": m.get("message_type", "text"),
                "delivery_status": m.get("delivery_status", "delivered"),
            }
            for m in messages
        ],
    })


async def _handle_search_messages(args: dict) -> list[TextContent]:
    """Full-text search across message history.

    Args:
        args: query (str), optional limit.

    Returns:
        JSON list of matching messages.
    """
    query: str = args.get("query", "")
    if not query:
        return _error("query is required")

    limit: int = args.get("limit", 20)
    history = _get_history()

    results = history.search_messages(query, limit=limit)

    return _json({
        "query": query,
        "count": len(results),
        "results": [
            {
                "id": r.get("id", ""),
                "sender": r.get("sender", ""),
                "content": r.get("content", "")[:500],
                "timestamp": r.get("timestamp", ""),
                "thread_id": r.get("thread_id"),
            }
            for r in results
        ],
    })


# ─────────────────────────────────────────────────────────────
# Tool Handlers — Groups
# ─────────────────────────────────────────────────────────────


async def _handle_create_group(args: dict) -> list[TextContent]:
    """Create a new group chat.

    Args:
        args: name, optional description, members list.

    Returns:
        JSON with group details.
    """
    name: str = args.get("name", "")
    if not name:
        return _error("name is required")

    description: str = args.get("description", "")
    members_raw: list[dict] = args.get("members", [])
    creator = _get_identity()

    group = GroupChat.create(
        name=name,
        creator_uri=creator,
        description=description,
    )

    # Add members
    added_members = []
    for m in members_raw:
        identity = m.get("identity", "")
        if not identity:
            continue

        # Resolve short names
        if not identity.startswith("capauth:"):
            try:
                from .identity_bridge import resolve_peer_name
                identity = resolve_peer_name(identity)
            except Exception:
                pass

        role_str = m.get("role", "member")
        try:
            role = MemberRole(role_str)
        except ValueError:
            role = MemberRole.MEMBER

        pt_str = m.get("participant_type", "agent")
        try:
            pt = ParticipantType(pt_str)
        except ValueError:
            pt = ParticipantType.AGENT

        member = group.add_member(
            identity_uri=identity,
            role=role,
            participant_type=pt,
            display_name=m.get("display_name", ""),
        )
        if member:
            added_members.append(identity)

    # Store in memory
    _groups[group.id] = group

    logger.info("Created group '%s' with %d members", name, len(group.members))

    return _json({
        "group_id": group.id,
        "name": group.name,
        "description": group.description,
        "member_count": len(group.members),
        "members": [
            {
                "identity": gm.identity_uri,
                "role": gm.role.value,
                "participant_type": gm.participant_type.value,
            }
            for gm in group.members
        ],
        "created_by": creator,
    })


async def _handle_group_send(args: dict) -> list[TextContent]:
    """Send a message to a group.

    Args:
        args: group_id, content.

    Returns:
        JSON with delivery result.
    """
    group_id: str = args.get("group_id", "")
    content: str = args.get("content", "")

    if not group_id:
        return _error("group_id is required")
    if not content:
        return _error("content is required")

    group = _groups.get(group_id)
    if group is None:
        return _error(f"Group not found: {group_id}")

    sender = _get_identity()
    message = group.compose_group_message(
        sender_uri=sender,
        content=content,
    )

    if message is None:
        return _error("Failed to compose group message (not a member?)")

    # Store in history
    history = _get_history()
    memory_id = history.store_message(message)

    return _json({
        "sent": True,
        "message_id": message.id,
        "group_id": group_id,
        "group_name": group.name,
        "recipient_count": len(group.members),
        "memory_id": memory_id,
    })


async def _handle_group_members(args: dict) -> list[TextContent]:
    """List members of a group.

    Args:
        args: group_id.

    Returns:
        JSON list of members.
    """
    group_id: str = args.get("group_id", "")
    if not group_id:
        return _error("group_id is required")

    group = _groups.get(group_id)
    if group is None:
        return _error(f"Group not found: {group_id}")

    return _json({
        "group_id": group_id,
        "group_name": group.name,
        "member_count": len(group.members),
        "members": [
            {
                "identity": m.identity_uri,
                "display_name": m.display_name,
                "role": m.role.value,
                "participant_type": m.participant_type.value,
                "joined_at": m.joined_at.isoformat(),
                "tool_scope": m.tool_scope,
            }
            for m in group.members
        ],
    })


async def _handle_group_add_member(args: dict) -> list[TextContent]:
    """Add a member to a group.

    Args:
        args: group_id, identity, optional role, participant_type.

    Returns:
        JSON with member details.
    """
    group_id: str = args.get("group_id", "")
    identity: str = args.get("identity", "")

    if not group_id:
        return _error("group_id is required")
    if not identity:
        return _error("identity is required")

    group = _groups.get(group_id)
    if group is None:
        return _error(f"Group not found: {group_id}")

    # Resolve short name
    if not identity.startswith("capauth:"):
        try:
            from .identity_bridge import resolve_peer_name
            identity = resolve_peer_name(identity)
        except Exception:
            pass

    role_str = args.get("role", "member")
    try:
        role = MemberRole(role_str)
    except ValueError:
        role = MemberRole.MEMBER

    pt_str = args.get("participant_type", "agent")
    try:
        pt = ParticipantType(pt_str)
    except ValueError:
        pt = ParticipantType.AGENT

    member = group.add_member(
        identity_uri=identity,
        role=role,
        participant_type=pt,
    )

    if member is None:
        return _error(f"Could not add member (already exists?): {identity}")

    return _json({
        "added": True,
        "group_id": group_id,
        "identity": identity,
        "role": role.value,
        "participant_type": pt.value,
        "member_count": len(group.members),
    })


# ─────────────────────────────────────────────────────────────
# Tool Handlers — Threads
# ─────────────────────────────────────────────────────────────


async def _handle_list_threads(args: dict) -> list[TextContent]:
    """List conversation threads.

    Args:
        args: Optional limit.

    Returns:
        JSON list of thread summaries.
    """
    limit: int = args.get("limit", 20)
    history = _get_history()

    threads = history.list_threads(limit=limit)

    return _json({
        "count": len(threads),
        "threads": [
            {
                "id": t.get("id", ""),
                "title": t.get("title"),
                "participants": t.get("participants", []),
                "message_count": t.get("message_count", 0),
                "created_at": t.get("created_at", ""),
                "updated_at": t.get("updated_at", ""),
            }
            for t in threads
        ],
    })


async def _handle_get_thread(args: dict) -> list[TextContent]:
    """Get messages from a thread.

    Args:
        args: thread_id, optional limit.

    Returns:
        JSON list of messages.
    """
    thread_id: str = args.get("thread_id", "")
    if not thread_id:
        return _error("thread_id is required")

    limit: int = args.get("limit", 50)
    history = _get_history()

    messages = history.get_thread_messages(thread_id, limit=limit)

    return _json({
        "thread_id": thread_id,
        "count": len(messages),
        "messages": [
            {
                "id": m.get("id", ""),
                "sender": m.get("sender", ""),
                "content": m.get("content", "")[:500],
                "timestamp": m.get("timestamp", ""),
                "reply_to": m.get("reply_to"),
            }
            for m in messages
        ],
    })


# ─────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────


def main() -> None:
    """Run the SKChat MCP server on stdio transport."""
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    asyncio.run(_run_server())


async def _run_server() -> None:
    """Async entry point for the stdio MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    main()
