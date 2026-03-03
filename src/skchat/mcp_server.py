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
    typing_start              — Broadcast a typing indicator to a peer via SKComm
    typing_stop               — Broadcast a typing-stopped indicator to a peer via SKComm
    capture_to_memory         — Capture a conversation thread to skcapstone memory
    capture_chat_to_memory    — Capture recent messages as a skcapstone memory (session-aware)
    get_context_for_message   — Search skcapstone memories relevant to a query for AI context
    speak_message             — Read a message aloud using Piper TTS (local, sovereign)
    record_voice_message      — Record audio from microphone and transcribe with Whisper STT
    skchat_group_create       — Create a group from a flat list[str] of member identity URIs
    skchat_group_send         — Send to group; returns {status, delivered_to, failed}
    skchat_peers              — List known peers with presence state and capabilities
    skchat_set_presence       — Broadcast own presence state via file transport to ~/.skcomm/outbox/
    skchat_get_presence       — Query presence cache for all peers or a specific peer

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
import re
import threading
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

# Single lock guards all lazy-init blocks above. Double-checked locking
# pattern: fast path reads without the lock; slow path acquires and re-checks.
_init_lock = threading.Lock()

_GROUPS_DIR = pathlib.Path.home() / ".skchat" / "groups"

# Well-known identity → display name mappings for group history formatting.
_SENDER_DISPLAY: dict[str, str] = {
    "capauth:lumina@skworld.io": "Lumina",
    "capauth:opus@skworld.io": "Claude/Opus",
}

server = Server("skchat")


# ─────────────────────────────────────────────────────────────
# Lazy initialization
# ─────────────────────────────────────────────────────────────


def _get_identity() -> str:
    """Get or resolve the sovereign identity."""
    global _identity
    if _identity is None:
        with _init_lock:
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
        with _init_lock:
            if _history is None:
                _history = ChatHistory.from_config()
    return _history


def _get_messenger() -> AgentMessenger:
    """Get or initialize the AgentMessenger."""
    global _messenger
    if _messenger is None:
        with _init_lock:
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
        with _init_lock:
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
        with _init_lock:
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
                "Returns uptime_seconds, messages_sent, messages_received, "
                "outbox_pending, transport_status, webrtc_signaling_ok, "
                "last_heartbeat_at, and online_peer_count."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="typing_start",
            description=(
                "Broadcast a typing indicator to a peer via SKComm HEARTBEAT. "
                "Call this before starting to generate a response so the peer's "
                "chat UI can show a typing animation. Use typing_stop when done."
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
                    "thread_id": {
                        "type": "string",
                        "description": "Thread context for the typing indicator (optional).",
                    },
                },
                "required": ["recipient"],
            },
        ),
        Tool(
            name="capture_to_memory",
            description=(
                "Capture a conversation thread from SKChat history into skcapstone "
                "sovereign memory. Fetches the last 50 messages for the given thread, "
                "formats them as a transcript, and sends them to the skcapstone "
                "session_capture tool for long-term retention."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to capture.",
                    },
                    "min_importance": {
                        "type": "number",
                        "description": "Minimum importance threshold 0.0-1.0 (default: 0.5).",
                    },
                },
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="typing_stop",
            description=(
                "Broadcast a typing-stopped indicator to a peer via SKComm HEARTBEAT. "
                "Call this after finishing response generation to clear the typing "
                "animation on the peer's chat UI."
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
                    "thread_id": {
                        "type": "string",
                        "description": "Thread context for the typing indicator (optional).",
                    },
                },
                "required": ["recipient"],
            },
        ),
        Tool(
            name="get_group_history",
            description=(
                "Get the last N messages from a group chat thread. "
                "Returns sender display names for well-known identities."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "Group ID (e.g. 'd4f3281e-fa92-474c-a8cd-f0a2a4c31c33' for skworld-team).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum messages to return (default: 20).",
                    },
                },
                "required": ["group_id"],
            },
        ),
        Tool(
            name="send_to_group",
            description=(
                "Send a message to all members of a group chat. "
                "Supports optional TTL for auto-expiring messages. "
                "Returns delivered/failed counts."
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
                    "ttl": {
                        "type": "integer",
                        "description": "Optional seconds until auto-delete.",
                    },
                },
                "required": ["group_id", "content"],
            },
        ),
        Tool(
            name="capture_chat_to_memory",
            description=(
                "Capture recent chat messages as a skcapstone memory for future context. "
                "If thread_id is omitted, captures from all active threads. "
                "Stores messages formatted as '[sender] content' via the skcapstone "
                "memory_store tool with tags=['skchat','conversation'] and importance=0.7. "
                "Returns the number of threads captured and their memory IDs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to capture (omit to capture all active threads).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum messages per thread to include (default: 20).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_context_for_message",
            description=(
                "Search skcapstone memories relevant to a chat message for AI context injection. "
                "Queries the skcapstone memory_search tool and returns a formatted bullet list "
                "of the top 5 matching memories, ready to prime AI responses."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The chat message or topic to search context for.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="who_is_online",
            description=(
                "List all known peers with their current presence status. "
                "Returns a JSON array of {identity, display_name, last_seen, status} "
                "for every peer in the local presence cache plus well-known identities. "
                "status is 'online' (<2 min since last heartbeat), "
                "'away' (<10 min), or 'offline'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "max_age": {
                        "type": "integer",
                        "description": (
                            "Max seconds since last seen to include a peer "
                            "(default: 300 = 5 minutes)."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="speak_message",
            description=(
                "Read a message aloud using Piper TTS (local, sovereign). "
                "Requires the piper binary and a voice model installed at "
                "~/.local/share/piper/voices/. "
                "Gracefully no-ops if Piper is not installed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to synthesise and play aloud.",
                    },
                    "voice": {
                        "type": "string",
                        "description": (
                            "Piper voice name (e.g. 'en_US-lessac-medium'). "
                            "Defaults to en_US-lessac-medium."
                        ),
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="list_peers",
            description=(
                "List all known agent peers from the skcapstone peer store "
                "(~/.skcapstone/peers/). Returns name, resolved identity URI, "
                "trust_level, entity_type, and capabilities for each peer. "
                "Useful for discovering available agents before sending messages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "description": (
                            "Filter by entity type (e.g. 'ai-agent', 'human'). "
                            "Omit to return all peers."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="record_voice_message",
            description=(
                "Record a voice message via the system microphone and transcribe it "
                "with Whisper STT (local, sovereign — no cloud dependency). "
                "Returns the transcribed text. "
                "Requires arecord (alsa-utils) and openai-whisper installed. "
                "Gracefully returns an error dict if either dependency is missing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "duration": {
                        "type": "integer",
                        "description": "Recording length in seconds (default: 10).",
                    },
                    "whisper_model": {
                        "type": "string",
                        "description": (
                            "Whisper model size for transcription "
                            "(tiny/base/small/medium/large, default: base)."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="skchat_group_create",
            description=(
                "Create a new SKChat group with a flat list of member identity URIs. "
                "The calling agent becomes admin. Returns group_id, name, members, and created_at."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Group display name.",
                    },
                    "members": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of member identity URIs or short names "
                            "(e.g. ['capauth:lumina@skworld.io', 'opus'])."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional group description.",
                    },
                },
                "required": ["name", "members"],
            },
        ),
        Tool(
            name="skchat_group_send",
            description=(
                "Send a message to all members of a SKChat group. "
                "Returns delivered_to (list of URIs that received the message) "
                "and failed (list of URIs where delivery failed)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "Group ID to send to.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message content.",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Override thread ID (defaults to group_id).",
                    },
                },
                "required": ["group_id", "message"],
            },
        ),
        Tool(
            name="skchat_send",
            description=(
                "Send a message to a recipient using AgentMessenger. "
                "Stores the message in sovereign history and delivers via SKComm "
                "transport when available. Returns status, message_id, delivered, "
                "and recipient."
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
                    "message": {
                        "type": "string",
                        "description": "Message text (markdown supported).",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to group the message in (optional).",
                    },
                    "message_type": {
                        "type": "string",
                        "enum": ["text", "finding", "task", "query", "response"],
                        "description": "Structured message type (default: text).",
                    },
                },
                "required": ["recipient", "message"],
            },
        ),
        Tool(
            name="skchat_set_presence",
            description=(
                "Broadcast your own presence state to peers via the SKComm file transport "
                "(~/.skcomm/outbox/). Valid states: online, offline, away, do-not-disturb, typing. "
                "Optionally attach a custom_status text (e.g. 'In a meeting'). "
                "Also updates the local presence cache so skchat_get_presence reflects the change. "
                "Returns {ok: bool}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["online", "offline", "away", "do-not-disturb", "typing"],
                        "description": "Presence state to broadcast.",
                    },
                    "custom_status": {
                        "type": "string",
                        "description": "Optional freeform status text (e.g. 'In a meeting').",
                    },
                },
                "required": ["state"],
            },
        ),
        Tool(
            name="skchat_get_presence",
            description=(
                "Query the local presence cache. "
                "If peer is given, returns the single entry for that identity URI. "
                "Otherwise returns all cached entries. "
                "Each entry has {uri, state, last_seen}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "peer": {
                        "type": "string",
                        "description": (
                            "CapAuth identity URI to look up (e.g. 'capauth:lumina@skworld.io'). "
                            "Omit to return all cached peers."
                        ),
                    },
                },
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
        "typing_start": _handle_typing_start,
        "typing_stop": _handle_typing_stop,
        "capture_to_memory": _handle_capture_to_memory,
        "get_group_history": _handle_get_group_history,
        "send_to_group": _handle_send_to_group,
        "capture_chat_to_memory": _handle_capture_chat_to_memory,
        "get_context_for_message": _handle_get_context_for_message,
        "who_is_online": _handle_who_is_online,
        "speak_message": _handle_speak_message,
        "list_peers": _handle_list_peers,
        "record_voice_message": _handle_record_voice_message,
        "skchat_group_create": _handle_skchat_group_create,
        "skchat_group_send": _handle_skchat_group_send,
        "skchat_send": _handle_skchat_send,
        "skchat_set_presence": _handle_skchat_set_presence,
        "skchat_get_presence": _handle_skchat_get_presence,
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
# Tool Handlers — Peer Discovery
# ─────────────────────────────────────────────────────────────


async def _handle_list_peers(args: dict) -> list[TextContent]:
    """List peers from the skcapstone peer store.

    Args:
        args: Optional entity_type filter.

    Returns:
        JSON list of {name, uri, trust_level, entity_type, capabilities}.
    """
    from .peer_discovery import PeerDiscovery

    disc = PeerDiscovery()
    peers = disc.list_peers()

    entity_type: str | None = args.get("entity_type")
    if entity_type:
        peers = [p for p in peers if p.get("entity_type", "").lower() == entity_type.lower()]

    result = []
    for peer in peers:
        handle = peer.get("handle", peer.get("name", ""))
        uri = disc.resolve_identity(handle) or handle
        result.append({
            "name": peer.get("name", ""),
            "uri": uri,
            "trust_level": peer.get("trust_level", ""),
            "entity_type": peer.get("entity_type", ""),
            "capabilities": peer.get("capabilities", []),
            "fingerprint": peer.get("fingerprint", ""),
            "last_seen": peer.get("last_seen"),
            "notes": peer.get("notes", ""),
        })

    return _json({
        "count": len(result),
        "peers": result,
    })


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

    # Extract @mentions before composing
    mentions = re.findall(r"@(\w+)", content)

    sender = _get_identity()
    message = group.compose_group_message(
        sender_uri=sender,
        content=content,
    )

    if message is None:
        return _error("Failed to compose group message (not a member?)")

    if mentions:
        message.metadata["mentions"] = mentions

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
# Tool Handlers — Typing Indicators
# ─────────────────────────────────────────────────────────────


def _send_typing_indicator(recipient: str, typing: bool, thread_id: Optional[str]) -> bool:
    """Send a typing presence indicator over SKComm HEARTBEAT.

    Args:
        recipient: Resolved CapAuth identity URI.
        typing: True to send TYPING state, False to send ONLINE (stopped).
        thread_id: Optional thread context.

    Returns:
        bool: True if the indicator was delivered, False on transport error.
    """
    from .presence import PresenceIndicator, PresenceState

    identity = _get_identity()
    state = PresenceState.TYPING if typing else PresenceState.ONLINE
    indicator = PresenceIndicator(
        identity_uri=identity,
        state=state,
        thread_id=thread_id,
    )

    try:
        from skcomm import SKComm
        from skcomm.models import MessageType

        comm = SKComm.from_config()
        comm.send(
            recipient=recipient,
            message=indicator.model_dump_json(),
            message_type=MessageType.HEARTBEAT,
        )
        return True
    except Exception as exc:
        logger.warning("Failed to send typing indicator to %s: %s", recipient[:24], exc)
        return False


async def _handle_typing_start(args: dict) -> list[TextContent]:
    """Broadcast a typing indicator to a peer.

    Sends a HEARTBEAT message with PresenceState.TYPING so the peer's
    UI can display a typing animation while the agent generates a response.

    Args:
        args: recipient (str), optional thread_id (str).

    Returns:
        JSON with typing status and delivery result.
    """
    recipient: str = args.get("recipient", "")
    if not recipient:
        return _error("recipient is required")

    thread_id: Optional[str] = args.get("thread_id")

    if not recipient.startswith("capauth:"):
        try:
            from .identity_bridge import resolve_peer_name
            recipient = resolve_peer_name(recipient)
        except Exception:
            pass

    sent = _send_typing_indicator(recipient, typing=True, thread_id=thread_id)

    return _json({
        "typing": True,
        "recipient": recipient,
        "thread_id": thread_id,
        "sent": sent,
    })


async def _handle_typing_stop(args: dict) -> list[TextContent]:
    """Broadcast a typing-stopped indicator to a peer.

    Sends a HEARTBEAT message with PresenceState.ONLINE to clear the
    typing animation on the peer's UI after response generation is complete.

    Args:
        args: recipient (str), optional thread_id (str).

    Returns:
        JSON with typing status and delivery result.
    """
    recipient: str = args.get("recipient", "")
    if not recipient:
        return _error("recipient is required")

    thread_id: Optional[str] = args.get("thread_id")

    if not recipient.startswith("capauth:"):
        try:
            from .identity_bridge import resolve_peer_name
            recipient = resolve_peer_name(recipient)
        except Exception:
            pass

    sent = _send_typing_indicator(recipient, typing=False, thread_id=thread_id)

    return _json({
        "typing": False,
        "recipient": recipient,
        "thread_id": thread_id,
        "sent": sent,
    })


# ─────────────────────────────────────────────────────────────
# Tool Handlers — Daemon
# ─────────────────────────────────────────────────────────────


async def _handle_daemon_status(args: dict) -> list[TextContent]:
    """Get the SKChat daemon status with full runtime metrics.

    Returns uptime, message counts, outbox queue depth, transport health,
    WebRTC signaling state, last heartbeat time, and online peer count.

    Args:
        args: No arguments required.

    Returns:
        JSON with daemon status and runtime statistics.
    """
    from .daemon import daemon_status

    status = daemon_status()

    # Enrich with live outbox pending count from skcomm PersistentOutbox.
    try:
        from skcomm.outbox import PersistentOutbox
        status["outbox_pending"] = PersistentOutbox().pending_count
    except Exception:
        status.setdefault("outbox_pending", 0)

    return _json(status)




# ─────────────────────────────────────────────────────────────
# Tool Handlers — Group History & send_to_group
# ─────────────────────────────────────────────────────────────


def _sender_display(identity_uri: str) -> str:
    """Map a CapAuth identity URI to a human-readable display name."""
    if identity_uri in _SENDER_DISPLAY:
        return _SENDER_DISPLAY[identity_uri]
    # Fall back to the local-part after the last colon
    return identity_uri.split(":")[-1] if ":" in identity_uri else identity_uri


async def _handle_get_group_history(args: dict) -> list[TextContent]:
    """Get the last N messages from a group chat thread.

    Args:
        args: group_id (str), optional limit (int, default 20).

    Returns:
        JSON list of messages with sender, sender_display, content, timestamp.
    """
    group_id: str = args.get("group_id", "")
    if not group_id:
        return _error("group_id is required")

    group = _get_groups().get(group_id)
    if group is None:
        return _error(f"Group not found: {group_id}")

    limit: int = args.get("limit", 20)
    history = _get_history()
    messages = history.get_thread_messages(group_id, limit=limit)

    return _json([
        {
            "sender": m.get("sender", ""),
            "sender_display": _sender_display(m.get("sender", "")),
            "content": m.get("content", ""),
            "timestamp": m.get("timestamp", ""),
        }
        for m in messages
    ])


async def _handle_send_to_group(args: dict) -> list[TextContent]:
    """Send a message to all members of a group chat.

    Supports optional TTL for auto-expiring messages.

    Args:
        args: group_id (str), content (str), optional ttl (int).

    Returns:
        JSON with delivered count, failed count, and message_id.
    """
    group_id: str = args.get("group_id", "")
    content: str = args.get("content", "")
    ttl: Optional[int] = args.get("ttl")

    if not group_id:
        return _error("group_id is required")
    if not content:
        return _error("content is required")

    group = _get_groups().get(group_id)
    if group is None:
        return _error(f"Group not found: {group_id}")

    # Extract @mentions (e.g. @lumina, @claude, @opus)
    mentions = re.findall(r"@(\w+)", content)

    sender = _get_identity()
    message = group.compose_group_message(
        sender_uri=sender,
        content=content,
        ttl=ttl,
    )

    if message is None:
        return _error("Failed to compose group message (not a member?)")

    if mentions:
        message.metadata["mentions"] = mentions

    history = _get_history()
    history.store_message(message)

    messenger = _get_messenger()
    delivered = 0
    failed = 0

    for member in group.members:
        if member.identity_uri == sender:
            continue
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
        except Exception:
            failed += 1

    _save_group(group)

    return _json({
        "message_id": message.id,
        "delivered": delivered,
        "failed": failed,
    })


async def _handle_skchat_group_create(args: dict) -> list[TextContent]:
    """Create a new group from a flat list of member identity URIs.

    Args:
        args: name (str), members (list[str]), optional description (str).

    Returns:
        JSON {group_id, name, members, created_at}.
    """
    name: str = args.get("name", "")
    if not name:
        return _error("name is required")

    members_raw: list[str] = args.get("members", [])
    if not isinstance(members_raw, list):
        return _error("members must be a list of identity URI strings")

    description: str = args.get("description", "")
    creator = _get_identity()

    group = GroupChat.create(
        name=name,
        creator_uri=creator,
        description=description,
    )

    added: list[str] = []
    for identity in members_raw:
        if not identity or not isinstance(identity, str):
            continue
        if not identity.startswith("capauth:"):
            try:
                from .identity_bridge import resolve_peer_name
                identity = resolve_peer_name(identity)
            except Exception:
                pass
        member = group.add_member(identity_uri=identity)
        if member:
            added.append(identity)

    _get_groups()[group.id] = group
    _save_group(group)

    logger.info("skchat_group_create: '%s' id=%s members=%d", name, group.id[:8], len(group.members))

    return _json({
        "group_id": group.id,
        "name": group.name,
        "members": [m.identity_uri for m in group.members],
        "created_at": group.created_at.isoformat(),
    })


async def _handle_skchat_group_send(args: dict) -> list[TextContent]:
    """Send a message to all members of a group.

    Args:
        args: group_id (str), message (str), optional thread_id (str).

    Returns:
        JSON {status, delivered_to: list[str], failed: list[str]}.
    """
    group_id: str = args.get("group_id", "")
    content: str = args.get("message", "")
    thread_id: Optional[str] = args.get("thread_id") or None

    if not group_id:
        return _error("group_id is required")
    if not content:
        return _error("message is required")

    group = _get_groups().get(group_id)
    if group is None:
        return _error(f"Group not found: {group_id}")

    sender = _get_identity()
    effective_thread = thread_id or group.id

    message = group.compose_group_message(
        sender_uri=sender,
        content=content,
    )
    if message is None:
        return _error("Failed to compose group message (not a member or observer?)")

    message.thread_id = effective_thread

    history = _get_history()
    history.store_message(message)

    messenger = _get_messenger()
    delivered_to: list[str] = []
    failed: list[str] = []

    for member in group.members:
        if member.identity_uri == sender:
            continue
        try:
            result = messenger.send(
                recipient=member.identity_uri,
                content=content,
                message_type="text",
                thread_id=effective_thread,
            )
            if result.get("delivered"):
                delivered_to.append(member.identity_uri)
            else:
                failed.append(member.identity_uri)
        except Exception:
            failed.append(member.identity_uri)

    _save_group(group)

    status = "ok" if not failed else ("partial" if delivered_to else "failed")
    return _json({
        "status": status,
        "delivered_to": delivered_to,
        "failed": failed,
    })


# -----------------------------------------------------------------
# Tool Handlers -- Memory Bridge
# -----------------------------------------------------------------


async def _handle_capture_to_memory(args: dict) -> list[TextContent]:
    """Capture a thread to skcapstone sovereign memory.

    Args:
        args: thread_id (str), optional min_importance (float).

    Returns:
        JSON with capture result (captured_count, skipped_count, etc.).
    """
    thread_id: str = args.get("thread_id", "")
    if not thread_id:
        return _error("thread_id is required")

    min_importance: float = float(args.get("min_importance", 0.5))

    from .memory_bridge import MemoryBridge

    bridge = MemoryBridge(history=_get_history())
    result = bridge.capture_thread(thread_id, min_importance=min_importance)

    if "error" in result:
        return _error(result["error"])

    return _json({
        "captured": True,
        "thread_id": thread_id,
        "min_importance": min_importance,
        **result,
    })

# -----------------------------------------------------------------
# Tool Handlers -- Session-aware Memory Capture & Context Retrieval
# -----------------------------------------------------------------


def _call_skcapstone(tool_name: str, arguments: dict) -> dict:
    """POST a tools/call request to the skcapstone MCP HTTP server.

    Args:
        tool_name: Name of the skcapstone tool to call (e.g. 'memory_store').
        arguments: Tool arguments dict.

    Returns:
        dict: Unwrapped tool result or {"error": "..."} on failure.
    """
    import json as _json_mod
    import urllib.error
    import urllib.request

    from .memory_bridge import SKCAPSTONE_MCP_URL, _CAPTURE_TIMEOUT

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    body = _json_mod.dumps(payload).encode()
    req = urllib.request.Request(
        SKCAPSTONE_MCP_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_CAPTURE_TIMEOUT) as resp:
            raw = resp.read().decode()
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("skcapstone MCP unavailable (%s): %s", tool_name, exc)
        return {"error": f"skcapstone unreachable: {exc}"}
    except Exception as exc:
        logger.warning("skcapstone HTTP call failed (%s): %s", tool_name, exc)
        return {"error": str(exc)}

    try:
        response = _json_mod.loads(raw)
    except _json_mod.JSONDecodeError as exc:
        return {"error": f"invalid JSON response: {exc}"}

    if "error" in response:
        return {"error": response["error"]}

    result = response.get("result", {})
    # MCP tools/call wraps output in result.content[0].text
    if isinstance(result, dict) and "content" in result:
        try:
            text = result["content"][0].get("text", "{}")
            return _json_mod.loads(text)
        except (KeyError, IndexError, _json_mod.JSONDecodeError):
            pass
    return result if isinstance(result, dict) else {"raw": result}


async def _handle_capture_chat_to_memory(args: dict) -> list[TextContent]:
    """Capture recent chat messages as a skcapstone memory for future context.

    If thread_id is None, captures from all active threads (up to 50).
    Messages are formatted as '[sender_short] content' before storage.

    Args:
        args: Optional thread_id (str), optional limit (int, default 20).

    Returns:
        JSON: {"captured": N, "memory_ids": [...]}
    """
    thread_id: Optional[str] = args.get("thread_id") or None
    limit: int = int(args.get("limit", 20))

    history = _get_history()

    # Determine target threads
    if thread_id:
        threads = [thread_id]
    else:
        thread_list = history.list_threads(limit=50)
        threads = [t.get("id") for t in thread_list if t.get("id")]

    if not threads:
        return _json({"captured": 0, "memory_ids": []})

    memory_ids: list[str] = []

    for tid in threads:
        messages = history.get_thread_messages(tid, limit=limit)
        if not messages:
            continue

        # Format as [sender_short] content (newest-last)
        lines: list[str] = []
        for msg in reversed(messages):
            sender = msg.get("sender") or "unknown"
            short = sender.split("@")[0].replace("capauth:", "")
            content = (msg.get("content") or "").strip()
            lines.append(f"[{short}] {content}")

        formatted = "\n".join(lines)

        result = _call_skcapstone(
            "memory_store",
            {
                "content": formatted,
                "tags": ["skchat", "conversation"],
                "importance": 0.7,
            },
        )

        if "error" in result:
            logger.warning(
                "capture_chat_to_memory: thread %s — %s", tid[:12], result["error"]
            )
            continue

        mem_id = result.get("memory_id") or result.get("id") or tid
        memory_ids.append(mem_id)

    return _json({"captured": len(memory_ids), "memory_ids": memory_ids})


async def _handle_get_context_for_message(args: dict) -> list[TextContent]:
    """Search skcapstone memories relevant to a chat message for AI context injection.

    Queries skcapstone memory_search (limit=5) and returns a formatted
    bullet-list string ready to prepend to an AI prompt.

    Args:
        args: query (str).

    Returns:
        JSON: {"context": "- ...\n- ...", "count": N}
    """
    query: str = args.get("query", "")
    if not query:
        return _error("query is required")

    result = _call_skcapstone("memory_search", {"query": query, "limit": 5})

    if "error" in result:
        logger.warning("get_context_for_message: %s", result["error"])
        return _json({"context": "", "count": 0, "error": result["error"]})

    memories = result.get("memories") or result.get("results") or []

    if not memories:
        return _json({"context": "", "count": 0})

    lines = [
        f"- {(m.get('content') or m.get('text') or '').strip()}"
        for m in memories
        if m.get("content") or m.get("text")
    ]
    context = "\n".join(lines)

    return _json({"context": context, "count": len(lines)})


# ─────────────────────────────────────────────────────────────
# Tool Handlers — Voice (Piper TTS)
# ─────────────────────────────────────────────────────────────


async def _handle_speak_message(args: dict) -> list[TextContent]:
    """Read a message aloud using Piper TTS.

    Args:
        args: Must contain ``text`` (str).  Optionally ``voice`` (str).

    Returns:
        JSON with ``spoken`` bool and ``available`` bool.  If Piper is not
        installed, returns ``available: false`` without an error so the
        caller can gracefully degrade.
    """
    text: str = args.get("text", "").strip()
    if not text:
        return _error("speak_message requires non-empty 'text'")

    from .voice import VoicePlayer, DEFAULT_VOICE

    voice: str = args.get("voice", DEFAULT_VOICE)
    player = VoicePlayer(voice=voice)

    if not player.is_available():
        logger.warning(
            "speak_message: Piper TTS not available (binary or voice model missing)"
        )
        return _json({"spoken": False, "available": False, "text": text})

    player.speak(text, blocking=False)
    return _json({"spoken": True, "available": True, "text": text, "voice": voice})


async def _handle_record_voice_message(args: dict) -> list[TextContent]:
    """Record a voice message and transcribe it with Whisper STT.

    Args:
        args: Optional ``duration`` (int, seconds, default 10) and
            ``whisper_model`` (str, default "base").

    Returns:
        JSON with ``transcribed`` bool, ``text`` str, and ``available`` bool.
        If openai-whisper or arecord is not installed, returns
        ``available: false`` with an install hint — no exception raised.
    """
    from .voice import VoiceRecorder

    duration: int = int(args.get("duration", 10))
    whisper_model: str = args.get("whisper_model", "base")

    recorder = VoiceRecorder(whisper_model=whisper_model)

    if not recorder.available:
        logger.warning(
            "record_voice_message: openai-whisper not installed"
        )
        return _json({
            "transcribed": False,
            "available": False,
            "text": "",
            "hint": "Install openai-whisper with: pip install openai-whisper",
        })

    text = recorder.record(duration=duration)

    if text is None:
        return _json({
            "transcribed": False,
            "available": True,
            "text": "",
            "hint": (
                "Recording failed — ensure arecord (alsa-utils) is installed "
                "and a microphone is available."
            ),
        })

    return _json({
        "transcribed": True,
        "available": True,
        "text": text,
        "duration": duration,
        "whisper_model": whisper_model,
    })


# ─────────────────────────────────────────────────────────────
# Tool Handlers — Presence
# ─────────────────────────────────────────────────────────────


async def _handle_who_is_online(args: dict) -> list[TextContent]:
    """List all known peers with their current presence status.

    Reads the local presence cache and enriches with display names from
    ~/.skcapstone/peers/ and the well-known-peers registry.

    Args:
        args: Optional max_age (int, seconds, default 300).

    Returns:
        JSON with count, online count, and peers list:
        [{identity, display_name, last_seen, status}].
    """
    from datetime import datetime, timezone

    from .presence import PresenceCache, PresenceState

    max_age: int = int(args.get("max_age", 300))

    KNOWN_PEERS: dict[str, str] = {
        "capauth:lumina@skworld.io": "lumina",
        "capauth:claude@skworld.io": "claude",
        "chef@skworld.io": "chef",
    }

    peers_dir = pathlib.Path.home() / ".skcapstone" / "peers"
    if peers_dir.exists():
        for peer_file in sorted(peers_dir.glob("*.json")):
            try:
                data = json.loads(peer_file.read_text())
                uri = data.get("identity_uri") or data.get("uri", "")
                name = data.get("display_name") or data.get("name", peer_file.stem)
                if uri:
                    KNOWN_PEERS[uri] = name
            except Exception:
                pass

    cache = PresenceCache()
    all_entries = cache.get_all()
    all_uris = sorted(set(all_entries.keys()) | set(KNOWN_PEERS.keys()))

    now = datetime.now(timezone.utc)
    peers_out: list[dict] = []
    for uri in all_uris:
        display = KNOWN_PEERS.get(uri, uri.split("@")[0].replace("capauth:", ""))
        entry = all_entries.get(uri)
        last_seen: Optional[str] = None
        if entry:
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                age = (now - ts).total_seconds()
                state_val = entry.get("state", "")
                if state_val == PresenceState.OFFLINE.value:
                    status = "offline"
                elif age <= 120:
                    status = "online"
                elif age <= max_age:
                    status = "away"
                else:
                    status = "offline"
                last_seen = ts.isoformat()
            except Exception:
                status = "offline"
        else:
            status = "offline"

        peers_out.append({
            "identity": uri,
            "display_name": display,
            "last_seen": last_seen,
            "status": status,
        })

    online_count = sum(1 for p in peers_out if p["status"] == "online")
    return _json({
        "count": len(peers_out),
        "online": online_count,
        "peers": peers_out,
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
