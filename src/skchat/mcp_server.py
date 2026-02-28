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
    list_groups        — List all group chats
    list_threads       — List conversation threads
    get_thread         — Get messages in a thread
    add_reaction       — Add an emoji reaction to a message
    remove_reaction    — Remove a reaction from a message
    daemon_status      — Get SKChat background daemon status

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
import pathlib
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
from .reactions import ReactionManager

logger = logging.getLogger("skchat.mcp")

# Module-level singletons — lazy init on first tool call.
_identity: Optional[str] = None
_history: Optional[ChatHistory] = None
_messenger: Optional[AgentMessenger] = None
_groups: dict[str, GroupChat] = {}
_groups_loaded: bool = False
_reactions: Optional[ReactionManager] = None

_GROUPS_DIR = pathlib.Path.home() / ".skchat" / "groups"

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


def _get_reactions() -> ReactionManager:
    """Get or initialize the ReactionManager."""
    global _reactions
    if _reactions is None:
        _reactions = ReactionManager()
    return _reactions


# ─────────────────────────────────────────────────────────────
# Group persistence
# ─────────────────────────────────────────────────────────────


def _get_groups() -> dict[str, GroupChat]:
    """Return the group registry, loading from disk on first call."""
    global _groups, _groups_loaded
    if not _groups_loaded:
        _groups_loaded = True
        _load_groups_from_disk()
    return _groups


def _load_groups_from_disk() -> None:
    """Populate _groups from ~/.skchat/groups/*.json."""
    global _groups
    try:
        _GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    for path in _GROUPS_DIR.glob("*.json"):
        try:
            group = GroupChat.model_validate_json(path.read_text(encoding="utf-8"))
            _groups[group.id] = group
        except Exception:
            logger.warning("Failed to load group from %s", path)


def _save_group(group: GroupChat) -> None:
    """Write a group to ~/.skchat/groups/{group_id}.json."""
    try:
        _GROUPS_DIR.mkdir(parents=True, exist_ok=True)
        path = _GROUPS_DIR / f"{group.id}.json"
        path.write_text(group.model_dump_json(indent=2), encoding="utf-8")
    except Exception:
        logger.warning("Failed to save group %s to disk", group.id[:8])


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
        Tool(
            name="webrtc_status",
            description=(
                "Get the status of WebRTC P2P connections. "
                "Lists active peer data channels, signaling state, and transport health."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="initiate_call",
            description=(
                "Initiate a WebRTC P2P connection to a peer agent or browser client. "
                "Sends a signaling message via SKComm to start ICE negotiation. "
                "Use webrtc_status after ~3s to confirm the connection is established."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "peer": {
                        "type": "string",
                        "description": (
                            "Peer fingerprint or agent name to connect to "
                            "(e.g. 'lumina' or 'CCBE9306410CF8CD5E393D6DEC31663B95230684')."
                        ),
                    },
                    "signaling_url": {
                        "type": "string",
                        "description": (
                            "Override the signaling broker URL for this call "
                            "(optional, uses configured default)."
                        ),
                    },
                },
                "required": ["peer"],
            },
        ),
        Tool(
            name="accept_call",
            description=(
                "Accept an incoming WebRTC call from a peer. "
                "Retrieves the pending SDP offer from the inbox and sends an answer."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "peer": {
                        "type": "string",
                        "description": "Fingerprint or name of the peer whose call to accept.",
                    },
                },
                "required": ["peer"],
            },
        ),
        Tool(
            name="send_file_p2p",
            description=(
                "Send a file directly to a peer via WebRTC data channels. "
                "Uses parallel channels for large files (up to 16 channels, "
                "similar to Weblink's approach). Falls back to SKComm transport if "
                "no direct WebRTC connection is available."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "peer": {
                        "type": "string",
                        "description": "Recipient peer fingerprint or agent name.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to send.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description of the file.",
                    },
                },
                "required": ["peer", "file_path"],
            },
        ),
        Tool(
            name="add_reaction",
            description=(
                "Add an emoji or text reaction to a message. "
                "Deduplicates: the same sender+emoji on the same message is only counted once."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to react to.",
                    },
                    "emoji": {
                        "type": "string",
                        "description": "Reaction emoji or short text (e.g. 'thumbsup', '❤️').",
                    },
                    "sender": {
                        "type": "string",
                        "description": (
                            "CapAuth identity URI of the reactor "
                            "(defaults to the sovereign identity if omitted)."
                        ),
                    },
                },
                "required": ["message_id", "emoji"],
            },
        ),
        Tool(
            name="remove_reaction",
            description=(
                "Remove an existing reaction from a message. "
                "Returns whether the reaction was found and removed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to remove the reaction from.",
                    },
                    "emoji": {
                        "type": "string",
                        "description": "The emoji or text reaction to remove.",
                    },
                    "sender": {
                        "type": "string",
                        "description": (
                            "CapAuth identity URI of the reactor "
                            "(defaults to the sovereign identity if omitted)."
                        ),
                    },
                },
                "required": ["message_id", "emoji"],
            },
        ),
        Tool(
            name="list_groups",
            description=(
                "List all group chats. Returns group IDs, names, "
                "descriptions, member counts, and creation times."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="daemon_status",
            description=(
                "Get the status of the SKChat background daemon. "
                "Returns whether it is running, its PID, and log file path."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
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
        "webrtc_status": _handle_webrtc_status,
        "initiate_call": _handle_initiate_call,
        "accept_call": _handle_accept_call,
        "send_file_p2p": _handle_send_file_p2p,
        "add_reaction": _handle_add_reaction,
        "remove_reaction": _handle_remove_reaction,
        "list_groups": _handle_list_groups,
        "daemon_status": _handle_daemon_status,
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

    # Register and persist
    _get_groups()[group.id] = group
    _save_group(group)

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

    Stores the composed message in local history, then delivers it
    to every group member (excluding the sender) via SKComm.

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

    group = _get_groups().get(group_id)
    if group is None:
        return _error(f"Group not found: {group_id}")

    sender = _get_identity()
    message = group.compose_group_message(
        sender_uri=sender,
        content=content,
    )

    if message is None:
        return _error("Failed to compose group message (not a member?)")

    # Store in local history
    history = _get_history()
    memory_id = history.store_message(message)

    # Deliver to each member via SKComm
    messenger = _get_messenger()
    delivered = 0
    failed = 0
    delivery_errors: list[str] = []

    for member in group.members:
        if member.identity_uri == sender:
            continue  # skip self
        try:
            result = messenger.send(
                recipient=member.identity_uri,
                content=content,
                message_type="text",
                thread_id=group.id,
            )
            if result.get("delivered"):
                delivered += 1
            else:
                failed += 1
                err = result.get("error")
                if err:
                    delivery_errors.append(f"{member.identity_uri[:24]}: {err}")
        except Exception as exc:
            failed += 1
            delivery_errors.append(f"{member.identity_uri[:24]}: {exc}")

    # Persist updated message_count
    _save_group(group)

    response: dict[str, Any] = {
        "sent": True,
        "message_id": message.id,
        "group_id": group_id,
        "group_name": group.name,
        "recipient_count": len(group.members) - 1,  # excludes self
        "delivered": delivered,
        "failed": failed,
        "memory_id": memory_id,
    }
    if delivery_errors:
        response["errors"] = delivery_errors
    return _json(response)


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

    group = _get_groups().get(group_id)
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

    group = _get_groups().get(group_id)
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

    _save_group(group)

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
# Tool Handlers — WebRTC
# ─────────────────────────────────────────────────────────────


def _get_webrtc_transport():
    """Lazy-get the WebRTC transport from the SKComm router.

    Returns:
        WebRTCTransport instance, or None if not configured.
    """
    try:
        from skcomm import SKComm

        comm = SKComm.from_config()
        for t in comm.router.transports:
            if t.name == "webrtc":
                return t
    except Exception:
        pass
    return None


async def _handle_webrtc_status(args: dict) -> list[TextContent]:
    """Get WebRTC transport status and active peer connections.

    Args:
        args: No arguments required.

    Returns:
        JSON with transport health and connected peer info.
    """
    transport = _get_webrtc_transport()

    if transport is None:
        return _json({
            "available": False,
            "reason": "WebRTC transport not configured. Add webrtc to ~/.skcomm/config.yml and pip install 'skcomm[webrtc]'.",
        })

    health = transport.health_check()

    peers_info: dict = {}
    try:
        with transport._peers_lock:
            for fp, peer in transport._peers.items():
                peers_info[fp[:8]] = {
                    "connected": peer.connected,
                    "negotiating": peer.negotiating,
                    "channel_open": peer.channel is not None,
                    "pending_count": len(peer.pending),
                }
    except Exception:
        pass

    return _json({
        "available": transport.is_available(),
        "running": transport._running,
        "signaling_connected": transport._signaling_connected,
        "signaling_url": transport._signaling_url,
        "status": health.status.value,
        "active_peers": peers_info,
        "inbox_pending": transport._inbox.qsize(),
        "error": health.error,
    })


async def _handle_initiate_call(args: dict) -> list[TextContent]:
    """Initiate a WebRTC P2P connection to a peer.

    Args:
        args: peer (str), optional signaling_url (str).

    Returns:
        JSON with initiation status and next steps.
    """
    peer: str = args.get("peer", "")
    if not peer:
        return _error("peer is required")

    transport = _get_webrtc_transport()
    if transport is None:
        return _error(
            "WebRTC transport not configured. "
            "Install aiortc: pip install 'skcomm[webrtc]' and enable webrtc in ~/.skcomm/config.yml"
        )

    if not transport._running:
        transport.start()

    # Resolve short name to fingerprint if possible
    if not peer.startswith("capauth:") and len(peer) != 40:
        try:
            from .identity_bridge import resolve_peer_name
            peer_uri = resolve_peer_name(peer)
            peer = peer_uri
        except Exception:
            pass

    # Schedule the WebRTC offer (non-blocking)
    transport._schedule_offer(peer)

    return _json({
        "initiated": True,
        "peer": peer,
        "message": (
            f"ICE negotiation started with {peer[:12]}. "
            "Check webrtc_status in ~3s to confirm the connection is established."
        ),
        "signaling_url": transport._signaling_url,
    })


async def _handle_accept_call(args: dict) -> list[TextContent]:
    """Accept an incoming WebRTC call from a peer.

    Looks for a WEBRTC_SIGNAL message in the recent inbox from the
    specified peer and processes it as an incoming SDP offer.

    Args:
        args: peer (str) - fingerprint or name of the calling peer.

    Returns:
        JSON with acceptance status.
    """
    peer: str = args.get("peer", "")
    if not peer:
        return _error("peer is required")

    transport = _get_webrtc_transport()
    if transport is None:
        return _error("WebRTC transport not configured")

    if not transport._running:
        transport.start()

    # Check if a connection already exists (may have been auto-accepted during negotiation)
    try:
        with transport._peers_lock:
            peer_conn = transport._peers.get(peer)
    except Exception:
        peer_conn = None

    if peer_conn and peer_conn.connected:
        return _json({
            "accepted": True,
            "peer": peer,
            "status": "already_connected",
            "message": f"Already connected to {peer[:12]} via WebRTC data channel.",
        })

    # Schedule an offer to the peer — if they already sent one,
    # the signaling broker will handle the SDP exchange
    transport._schedule_offer(peer)

    return _json({
        "accepted": True,
        "peer": peer,
        "status": "negotiating",
        "message": (
            f"WebRTC negotiation initiated with {peer[:12]}. "
            "Check webrtc_status in ~5s to confirm the connection."
        ),
    })


async def _handle_send_file_p2p(args: dict) -> list[TextContent]:
    """Send a file to a peer via WebRTC data channel or SKComm fallback.

    Uses FileSender (skchat.files) for 256KB-chunked, encrypted transfer so
    the full file is never loaded into memory at once.  Each FileChunk is sent
    as an individual message, preceded by a transfer-manifest message.

    Args:
        args: peer (str), file_path (str), optional description (str).

    Returns:
        JSON with transfer status.
    """
    peer: str = args.get("peer", "")
    file_path: str = args.get("file_path", "")
    description: str = args.get("description", "")

    if not peer:
        return _error("peer is required")
    if not file_path:
        return _error("file_path is required")

    import json as _json_mod
    from pathlib import Path

    from .files import FileSender

    path = Path(file_path).expanduser()
    if not path.exists():
        return _error(f"File not found: {file_path}")
    if not path.is_file():
        return _error(f"Not a file: {file_path}")

    file_size = path.stat().st_size
    file_name = path.name

    # Prepare chunked transfer metadata (reads file once, in 256KB blocks)
    sender = FileSender(sender_identity=_get_identity())
    transfer = sender.prepare(str(path), recipient=peer)
    chunks = sender.chunks(transfer, str(path))

    manifest_dict = {
        "type": "file_transfer",
        "transfer_id": transfer.transfer_id,
        "name": file_name,
        "size": file_size,
        "total_chunks": transfer.total_chunks,
        "sha256": transfer.sha256,
        "description": description,
    }

    # Try WebRTC direct channel first
    transport = _get_webrtc_transport()
    if transport and transport.is_available():
        try:
            with transport._peers_lock:
                peer_conn = transport._peers.get(peer)

            if peer_conn and peer_conn.connected and peer_conn.channel:
                # Send manifest then each chunk over the open data channel
                manifest_bytes = _json_mod.dumps(manifest_dict).encode()
                manifest_frame = len(manifest_bytes).to_bytes(4, "big") + manifest_bytes
                fut = asyncio.run_coroutine_threadsafe(
                    transport._async_channel_send(peer_conn.channel, manifest_frame),
                    transport._loop,
                )
                fut.result(timeout=10.0)

                for chunk in chunks:
                    chunk_bytes = chunk.to_json().encode()
                    fut = asyncio.run_coroutine_threadsafe(
                        transport._async_channel_send(peer_conn.channel, chunk_bytes),
                        transport._loop,
                    )
                    fut.result(timeout=30.0)

                return _json({
                    "sent": True,
                    "transport": "webrtc-direct",
                    "peer": peer,
                    "file": file_name,
                    "size_bytes": file_size,
                    "chunks": len(chunks),
                    "transfer_id": transfer.transfer_id,
                    "description": description,
                })
        except Exception as exc:
            logger.warning("WebRTC direct file send failed: %s — falling back to SKComm", exc)

    # Fallback: send via SKComm as chunked WEBRTC_FILE messages
    try:
        from skcomm import SKComm
        from skcomm.models import MessageType

        comm = SKComm.from_config()

        # Send manifest (includes encrypted_key for the receiver)
        manifest_dict["encrypted_key"] = transfer.encrypted_key
        comm.send(
            recipient=peer,
            message=_json_mod.dumps(manifest_dict),
            message_type=MessageType.WEBRTC_FILE,
        )

        for chunk in chunks:
            comm.send(
                recipient=peer,
                message=chunk.to_json(),
                message_type=MessageType.WEBRTC_FILE,
            )

        return _json({
            "sent": True,
            "transport": "skcomm-chunked",
            "peer": peer,
            "file": file_name,
            "size_bytes": file_size,
            "chunks": len(chunks),
            "transfer_id": transfer.transfer_id,
        })
    except Exception as exc:
        return _error(f"File send failed: {exc}")


# ─────────────────────────────────────────────────────────────
# Tool Handlers — Reactions
# ─────────────────────────────────────────────────────────────


async def _handle_add_reaction(args: dict) -> list[TextContent]:
    """Add a reaction to a message.

    Args:
        args: message_id, emoji, optional sender.

    Returns:
        JSON with the reaction summary for that message.
    """
    message_id: str = args.get("message_id", "")
    emoji: str = args.get("emoji", "")

    if not message_id:
        return _error("message_id is required")
    if not emoji:
        return _error("emoji is required")

    sender: str = args.get("sender") or _get_identity()

    manager = _get_reactions()
    added = manager.add_reaction(message_id, emoji, sender)
    summary = manager.summarize(message_id)

    return _json({
        "added": added,
        "message_id": message_id,
        "emoji": emoji,
        "sender": sender,
        "reactions": summary.reactions,
        "total_count": summary.total_count,
    })


async def _handle_remove_reaction(args: dict) -> list[TextContent]:
    """Remove a reaction from a message.

    Args:
        args: message_id, emoji, optional sender.

    Returns:
        JSON with removal status and updated reaction summary.
    """
    message_id: str = args.get("message_id", "")
    emoji: str = args.get("emoji", "")

    if not message_id:
        return _error("message_id is required")
    if not emoji:
        return _error("emoji is required")

    sender: str = args.get("sender") or _get_identity()

    manager = _get_reactions()
    removed = manager.remove_reaction(message_id, emoji, sender)
    summary = manager.summarize(message_id)

    return _json({
        "removed": removed,
        "message_id": message_id,
        "emoji": emoji,
        "sender": sender,
        "reactions": summary.reactions,
        "total_count": summary.total_count,
    })


# ─────────────────────────────────────────────────────────────
# Tool Handlers — Groups listing
# ─────────────────────────────────────────────────────────────


async def _handle_list_groups(args: dict) -> list[TextContent]:
    """List all group chats.

    Args:
        args: No arguments required.

    Returns:
        JSON list of group summaries.
    """
    groups = _get_groups()

    return _json({
        "count": len(groups),
        "groups": [
            {
                "id": g.id,
                "name": g.name,
                "description": g.description,
                "member_count": len(g.members),
                "created_at": g.created_at.isoformat() if hasattr(g, "created_at") and g.created_at else None,
            }
            for g in groups.values()
        ],
    })


# ─────────────────────────────────────────────────────────────
# Tool Handlers — Daemon
# ─────────────────────────────────────────────────────────────


async def _handle_daemon_status(args: dict) -> list[TextContent]:
    """Get the SKChat daemon status.

    Args:
        args: No arguments required.

    Returns:
        JSON with running state, PID, and file paths.
    """
    from .daemon import daemon_status

    status = daemon_status()
    return _json(status)


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
