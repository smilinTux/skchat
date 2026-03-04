"""SKChat CLI — sovereign encrypted chat from your terminal.

Commands:
    skchat send <recipient> <message>
    skchat chat <peer>
    skchat inbox [--limit N] [--since N] [--watch] [--interval S]
    skchat history <participant> [--limit N]
    skchat threads [--limit N]
    skchat search <query>
    skchat status
    skchat presence

All commands operate against the local SKMemory-backed chat history.
Messages are composed locally, stored via ChatHistory, and (when
transport is wired) sent via SKComm.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()
    HAS_RICH = True
except ImportError:
    console = None  # type: ignore[assignment]
    HAS_RICH = False

from . import __version__
from .agent_comm import AgentMessenger
from .models import ChatMessage, ContentType, DeliveryStatus, Thread
from .identity_bridge import (
    get_sovereign_identity,
    resolve_peer_name,
    PeerResolutionError,
)
from .reactions import ReactionStore
from .peer_discovery import PeerDiscovery


SKCHAT_HOME = "~/.skchat"

_reaction_store = ReactionStore()


def _suppress_pgp_warnings_if_configured() -> None:
    """Suppress PGPy UserWarnings if crypto.suppress_passphrase_warning is set.

    Reads ~/.skcomm/config.yml for::

        crypto:
          suppress_passphrase_warning: true

    Silently skips if the config is absent, unreadable, or PyYAML is missing.
    """
    import warnings

    try:
        import yaml  # type: ignore[import-untyped]

        config_path = Path.home() / ".skcomm" / "config.yml"
        if config_path.exists():
            with open(config_path) as _f:
                cfg = yaml.safe_load(_f) or {}
            if cfg.get("crypto", {}).get("suppress_passphrase_warning"):
                warnings.filterwarnings("ignore", category=UserWarning, module="pgpy")
    except Exception:
        pass


_suppress_pgp_warnings_if_configured()


def _get_chat_transport():
    """Create a ChatTransport instance for message delivery.

    Returns None if SKComm is not installed or has no transports.

    Returns:
        ChatTransport or None.
    """
    try:
        from skcomm.core import SKComm
        from .transport import ChatTransport

        comm = SKComm.from_config()
        if not comm.router.transports:
            return None

        history = _get_history()
        return ChatTransport(
            skcomm=comm,
            history=history,
            identity=_get_identity(),
        )
    except ImportError:
        return None
    except Exception:
        return None


def _print(msg: str) -> None:
    """Print using Rich if available, else plain click.echo.

    Args:
        msg: Message string (may contain Rich markup).
    """
    if HAS_RICH and console:
        console.print(msg)
    else:
        click.echo(msg)


def _get_identity() -> str:
    """Load the local user's identity URI from CapAuth sovereign profile.

    First checks environment variable SKCHAT_IDENTITY, then reads from
    ~/.skcapstone/identity/identity.json (CapAuth sovereign profile),
    then falls back to ~/.skchat/config.yml, and finally to a default.

    Returns:
        str: CapAuth identity URI for the local user.
    """
    try:
        return get_sovereign_identity()
    except Exception:
        import os

        identity = os.environ.get("SKCHAT_IDENTITY")
        if identity:
            return identity

        config_path = Path(SKCHAT_HOME).expanduser() / "config.yml"
        if config_path.exists():
            try:
                import yaml

                with open(config_path) as f:
                    cfg = yaml.safe_load(f)
                return cfg.get("skchat", {}).get("identity", {}).get("uri", "capauth:local@skchat")
            except Exception:
                pass

        return "capauth:local@skchat"


def _get_history() -> "ChatHistory":
    """Create a ChatHistory backed by SKMemory.

    Uses a dedicated SKMemory store at ~/.skchat/memory/ so chat
    data stays separate from other SKMemory data.

    Returns:
        ChatHistory: Ready-to-use chat history instance.
    """
    from .history import ChatHistory

    try:
        from skmemory import MemoryStore, SQLiteBackend

        store_path = Path(SKCHAT_HOME).expanduser() / "memory"
        store_path.mkdir(parents=True, exist_ok=True)
        backend = SQLiteBackend(base_path=str(store_path))
        store = MemoryStore(primary=backend)
    except ImportError:
        from skmemory import MemoryStore

        store = MemoryStore()

    return ChatHistory(store=store)


def _get_transport() -> "Optional[ChatTransport]":
    """Try to create a ChatTransport backed by SKComm.

    Returns None if SKComm is not installed or not configured,
    allowing graceful fallback to local-only storage.

    Returns:
        Optional[ChatTransport]: Transport bridge, or None.
    """
    try:
        from skcomm import SKComm

        from .transport import ChatTransport

        comm = SKComm.from_config()
        history = _get_history()
        identity = _get_identity()
        return ChatTransport(
            skcomm=comm,
            history=history,
            identity=identity,
        )
    except Exception:
        return None


def _send_typing_before_message(
    recipient: str,
    thread_id: "Optional[str]" = None,
    delay: float = 0.5,
) -> None:
    """Send a TYPING indicator to recipient, then pause briefly.

    Fire-and-forget: any transport errors are silently suppressed so
    a missing SKComm config never blocks the actual send.

    Args:
        recipient: Resolved CapAuth identity URI.
        thread_id: Optional thread context.
        delay: How long to wait (seconds) after sending indicator.
    """
    import time as _time

    transport = _get_transport()
    if transport is None:
        return
    try:
        transport.send_typing_indicator(recipient, thread_id=thread_id)
        _time.sleep(delay)
    except Exception:
        pass


def _try_deliver(msg: "ChatMessage") -> dict:
    """Attempt to deliver a message via SKComm transport.

    Falls back gracefully if SKComm is not available.

    Args:
        msg: The ChatMessage to deliver.

    Returns:
        dict: Delivery result with 'delivered' bool.
    """
    transport = _get_transport()
    if transport is None:
        return {"delivered": False, "error": "no transport", "transport": None}

    try:
        return transport.send_message(msg)
    except Exception as exc:
        return {"delivered": False, "error": str(exc), "transport": None}


@click.group()
@click.version_option(version=__version__, prog_name="skchat")
def main() -> None:
    """SKChat -- Sovereign Encrypted Chat.

    AI-native P2P communication. Your messages, your keys,
    your AI in the room.
    """


@main.command()
@click.argument("recipient")
@click.argument("message", required=False, default=None)
@click.option("--thread", "-t", default=None, help="Thread ID for conversation grouping.")
@click.option("--reply-to", "-r", default=None, help="Message ID this replies to.")
@click.option(
    "--ttl",
    type=int,
    default=None,
    help="Seconds until auto-delete (ephemeral message).",
)
@click.option(
    "--content-type",
    "ctype",
    type=click.Choice(["plain", "markdown"], case_sensitive=False),
    default="markdown",
    help="Content type (default: markdown).",
)
@click.option(
    "--voice",
    is_flag=True,
    default=False,
    help="Record a voice message via microphone and transcribe with Whisper STT.",
)
@click.option(
    "--whisper-model",
    "whisper_model",
    default="base",
    show_default=True,
    help="Whisper model for voice transcription (tiny/base/small/medium/large).",
)
def send(
    recipient: str,
    message: Optional[str],
    thread: Optional[str],
    reply_to: Optional[str],
    ttl: Optional[int],
    ctype: str,
    voice: bool,
    whisper_model: str,
) -> None:
    """Send a message to a recipient.

    Composes a ChatMessage, stores it in local history, and
    (when transport is available) queues it for delivery via SKComm.

    The recipient can be either a full capauth URI or a friendly peer name
    that will be resolved from the peer registry (e.g., "lumina" resolves
    to "capauth:lumina@capauth.local").

    Use --voice to record audio via microphone instead of typing.
    Requires arecord (alsa-utils) and openai-whisper
    (pip install openai-whisper).

    Examples:

        skchat send capauth:bob@skworld.io "Hey Bob!"

        skchat send lumina "Check this out" --thread abc123

        skchat send bob "Secret" --ttl 60

        skchat send lumina --voice

        skchat send lumina --voice --whisper-model small
    """
    # --voice: record audio, transcribe, confirm before sending
    if voice:
        from .voice import VoiceRecorder

        recorder = VoiceRecorder(whisper_model=whisper_model)
        if not recorder.available:
            _print("\n  [red]Error:[/] openai-whisper is not installed.")
            _print("  Install with: [cyan]pip install openai-whisper[/]\n")
            sys.exit(1)

        _print("\n  [cyan]Recording...[/] press Enter to stop\n")
        transcribed = recorder.record_interactive()

        if not transcribed:
            _print("\n  [red]No transcription.[/] Recording failed or captured silence.\n")
            sys.exit(1)

        _print(f"\n  [bold]Transcription:[/] {transcribed}")
        if not click.confirm("  Send?", default=True):
            _print("  [dim]Cancelled.[/]\n")
            return

        message = transcribed

    elif not message:
        _print("\n  [red]Error:[/] MESSAGE argument is required (or use --voice).\n")
        sys.exit(1)

    sender = _get_identity()

    # Resolve short name / @handle to a full identity URI.
    # PeerDiscovery searches all fields (name, handle, email, contact_uris).
    # Falls back to "{name}@skworld.io" if no peer record is found.
    resolved_recipient = PeerDiscovery().resolve_identity(recipient) or recipient

    # Guard: must look like an identity address (has "@" or a scheme ":")
    if "@" not in resolved_recipient and ":" not in resolved_recipient:
        _print(f"\n  [red]Error:[/] Cannot resolve [cyan]'{recipient}'[/] to a valid identity.")
        _print(f"  [yellow]Hint:[/] Register the peer with: [cyan]skcapstone peer add {recipient}[/]\n")
        sys.exit(1)

    content_type = ContentType.PLAIN if ctype == "plain" else ContentType.MARKDOWN

    msg = ChatMessage(
        sender=sender,
        recipient=resolved_recipient,
        content=message,
        content_type=content_type,
        thread_id=thread,
        reply_to_id=reply_to,
        ttl=ttl,
        delivery_status=DeliveryStatus.PENDING,
    )

    history = _get_history()
    mem_id = history.store_message(msg)

    # Send typing indicator so recipient sees "X is typing..." before the message arrives
    _send_typing_before_message(resolved_recipient, thread)

    transport_info = _try_deliver(msg)

    _print("")
    if HAS_RICH and console:
        if transport_info["delivered"]:
            status_str = f"[green]sent[/] via {transport_info['transport']}"
        else:
            status_str = f"[yellow]stored locally[/] ({transport_info.get('error', 'no transport')})"
        
        display_recipient = recipient if recipient == resolved_recipient else f"{recipient} ({resolved_recipient})"
        
        console.print(Panel(
            f"[bold]To:[/] [cyan]{display_recipient}[/]\n"
            f"[bold]Content:[/] {message[:120]}\n"
            f"[bold]Thread:[/] {thread or '[dim]none[/]'}\n"
            f"[bold]TTL:[/] {f'{ttl}s' if ttl else '[dim]permanent[/]'}\n"
            f"[bold]Status:[/] {status_str}\n"
            f"[dim]Memory ID: {mem_id}[/]",
            title="Message Sent",
            border_style="green",
        ))
    else:
        _print(f"  Sent to {resolved_recipient}: {message[:80]}")
        if transport_info["delivered"]:
            _print(f"  Delivered via {transport_info['transport']}")
        else:
            _print(f"  Stored locally ({transport_info.get('error', 'no transport')})")
        _print(f"  Memory ID: {mem_id}")
    _print("")


def _find_message_by_id(history: "ChatHistory", message_id: str) -> Optional[dict]:
    """Find a stored message by its memory ID or chat_message_id.

    Supports full UUID or a unique prefix (≥4 chars).

    Args:
        history: The ChatHistory to search.
        message_id: Full or prefix of memory_id or chat_message_id.

    Returns:
        Optional[dict]: Chat message dict, or None if not found.
    """
    try:
        all_mems = history._store.list_memories(tags=["skchat:message"], limit=5000)
    except Exception:
        return None
    for m in all_mems:
        if m.id == message_id or m.id.startswith(message_id):
            return history._memory_to_chat_dict(m)
        cid = m.metadata.get("chat_message_id", "")
        if cid and (cid == message_id or cid.startswith(message_id)):
            return history._memory_to_chat_dict(m)
    return None


@main.command()
@click.argument("message_id")
@click.argument("content")
@click.option("--thread", "-t", default=None, help="Thread ID (defaults to original thread).")
@click.option(
    "--content-type",
    "ctype",
    type=click.Choice(["plain", "markdown"], case_sensitive=False),
    default="markdown",
    help="Content type (default: markdown).",
)
def reply(message_id: str, content: str, thread: Optional[str], ctype: str) -> None:
    """Reply to a specific message.

    Looks up MESSAGE_ID in local history (memory ID or chat_message_id
    prefix), sets the original sender as recipient, and stores with
    reply_to_id set so clients can thread the conversation.

    Examples:

        skchat reply abc12345 "Good point, agreed!"

        skchat reply abc12345 "Follow-up question" --thread proj-alpha
    """
    history = _get_history()
    orig = _find_message_by_id(history, message_id)

    if orig is None:
        _print(f"\n  [red]Error:[/] Message [dim]{message_id}[/] not found in local history.")
        _print("  [yellow]Hint:[/] Get the Memory ID from [cyan]skchat send[/] output or [cyan]skchat inbox[/].\n")
        sys.exit(1)

    # Reply goes to whoever sent the original message
    recipient_uri = orig.get("sender") or ""
    if not recipient_uri:
        _print(f"\n  [red]Error:[/] Cannot determine sender of message {message_id}.\n")
        sys.exit(1)

    sender = _get_identity()
    thread_id = thread or orig.get("thread_id")
    content_type = ContentType.PLAIN if ctype == "plain" else ContentType.MARKDOWN

    msg = ChatMessage(
        sender=sender,
        recipient=recipient_uri,
        content=content,
        content_type=content_type,
        thread_id=thread_id,
        reply_to_id=orig.get("memory_id") or message_id,
        delivery_status=DeliveryStatus.PENDING,
    )

    mem_id = history.store_message(msg)
    transport_info = _try_deliver(msg)

    _print("")
    if HAS_RICH and console:
        status_str = (
            f"[green]sent[/] via {transport_info['transport']}"
            if transport_info["delivered"]
            else f"[yellow]stored locally[/] ({transport_info.get('error', 'no transport')})"
        )
        orig_preview = (orig.get("content") or "")[:60]
        console.print(Panel(
            f"[bold]Reply to:[/] [dim]{message_id[:12]}…[/] {orig_preview}\n"
            f"[bold]To:[/]       [cyan]{recipient_uri}[/]\n"
            f"[bold]Content:[/]  {content[:120]}\n"
            f"[bold]Status:[/]   {status_str}\n"
            f"[dim]Memory ID: {mem_id}[/]",
            title="Reply Sent",
            border_style="green",
        ))
    else:
        _print(f"  Reply to {message_id[:12]} → {recipient_uri}: {content[:80]}")
        _print(f"  Memory ID: {mem_id}")
    _print("")


# ─────────────── inbox display helpers ───────────────

def _display_name(identity: str) -> str:
    """Resolve a CapAuth identity URI to a friendly display name.

    Uses peer-store reverse lookup (name/fingerprint/URI → friendly name)
    before falling back to string parsing.  Never returns "unknown".

    Examples:
        'capauth:lumina@skworld.io'              → 'Lumina'
        'capauth:AABB1122CCDD3344EEFF5566...'    → 'Lumina'  (via peer store)
        'capauth:chef@skworld.io'                → 'Chef'
    """
    if not identity:
        return ""
    try:
        from .identity_bridge import resolve_display_name
        return resolve_display_name(identity)
    except Exception:
        pass
    # bare string fallback (no identity_bridge available)
    try:
        local = identity
        if ":" in local:
            local = local.split(":", 1)[1]
        if "@" in local:
            local = local.split("@", 1)[0]
        return local.capitalize() if local else identity
    except Exception:
        return identity


def _sender_color(sender: str, my_identity: str) -> str:
    """Return a Rich color name for a given sender.

    Mapping:
        self (my_identity) → blue
        *lumina*           → magenta
        *chef*             → yellow
        anyone else        → cyan
    """
    if sender == my_identity:
        return "blue"
    lower = sender.lower()
    if "lumina" in lower:
        return "magenta"
    if "chef" in lower:
        return "yellow"
    return "cyan"


_READ_STATE_PATH = Path("~/.skchat/read-state.json")


def _load_read_state() -> dict:
    """Load per-conversation last-read timestamps from disk.

    Returns:
        dict: Maps key strings to ISO timestamp strings.
    """
    path = _READ_STATE_PATH.expanduser()
    if path.exists():
        try:
            import json as _json
            return _json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_read_state(state: dict) -> None:
    """Persist per-conversation last-read timestamps to disk."""
    import json as _json
    path = _READ_STATE_PATH.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(state, indent=2))


def _ts_ago(ts: object) -> str:
    """Convert a timestamp to a human-readable relative string.

    Returns strings like '5s ago', '2min ago', '3h ago', '1d ago'.
    """
    try:
        if isinstance(ts, str):
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        elif isinstance(ts, datetime):
            ts_dt = ts
        else:
            return str(ts)[:16]
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts_dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}min ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return str(ts)[:16]


def _ts_hhmm(ts: object) -> str:
    """Extract HH:MM from a timestamp for compact inline display."""
    try:
        if isinstance(ts, str) and len(ts) >= 16:
            return ts[11:16]
        if hasattr(ts, "strftime"):
            return ts.strftime("%H:%M")
        return str(ts)[:5]
    except Exception:
        return ""


def _msg_key(msg: dict) -> str:
    """Build a stable deduplication key from a message dict.

    Tries dedicated ID fields first, falls back to a composite of
    sender + timestamp + content prefix.
    """
    return (
        msg.get("memory_id")
        or msg.get("message_id")
        or msg.get("id")
        or ":".join([
            str(msg.get("sender", "")),
            str(msg.get("timestamp", "")),
            str(msg.get("content", ""))[:30],
        ])
    )


def _inbox_display_table(messages: list) -> None:
    """Display inbox messages as a flat table: Time | From | Content (80 chars)."""
    if HAS_RICH and console:
        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=None,
            padding=(0, 1),
        )
        table.add_column("Time", style="dim", max_width=12, no_wrap=True)
        table.add_column("From", style="cyan", max_width=20, no_wrap=True)
        table.add_column("Content", ratio=1)
        for msg in messages:
            ts = _ts_ago(msg.get("timestamp", ""))
            sender = _display_name(msg.get("sender") or "")
            content = msg.get("content", "")
            preview = content[:80] + ("…" if len(content) > 80 else "")
            table.add_row(ts, sender, preview)
        console.print(table)
    else:
        for msg in messages:
            ts = _ts_ago(msg.get("timestamp", ""))
            sender = _display_name(msg.get("sender") or "")
            content = msg.get("content", "")[:80]
            click.echo(f"  [{ts}] {sender}: {content}")


def _inbox_display_grouped(messages: list, my_identity: str) -> None:
    """Display inbox messages grouped by conversation peer.

    Renders a cyan divider header per peer, then each message with a
    HH:MM timestamp.  Your messages appear in blue; peer colours vary
    by name.  @mentions are highlighted bold yellow.
    """
    from collections import OrderedDict

    sorted_msgs = sorted(messages, key=lambda m: str(m.get("timestamp", "")))

    peer_groups: dict = OrderedDict()
    for msg in sorted_msgs:
        sender = msg.get("sender") or ""
        recipient = msg.get("recipient") or ""
        peer = recipient if sender == my_identity else sender
        peer_groups.setdefault(peer, []).append(msg)

    term_width = 72
    for peer, msgs in peer_groups.items():
        name = _display_name(peer)
        peer_label = peer or "?"
        prefix = f"── {name} ({peer_label}) "
        dashes = "─" * max(4, term_width - len(prefix))
        header = prefix + dashes
        if HAS_RICH and console:
            console.print(f"[cyan]{header}[/]")
        else:
            click.echo(header)

        for msg in msgs:
            sender = msg.get("sender") or ""
            content = msg.get("content", "")
            ts_label = _ts_hhmm(msg.get("timestamp", ""))

            if sender == my_identity:
                label = "You"
                color = "blue"
            else:
                label = _display_name(sender)
                color = _sender_color(sender, my_identity)

            if HAS_RICH and "@" in content:
                content_display = re.sub(r"@(\w+)", r"[bold yellow]@\1[/]", content)
            else:
                content_display = content

            if HAS_RICH and console:
                console.print(
                    f"  [dim white]{ts_label}[/] [{color}]{label}:[/]"
                    f" [{color}]{content_display}[/]"
                )
            else:
                click.echo(f"  [{ts_label}] {label}: {content}")

            msg_id = msg.get("id", msg.get("message_id", ""))
            if msg_id:
                rx = _reaction_store.get_summary(msg_id)
                if rx:
                    rx_str = "  ".join(f"{e} {c}" for e, c in rx.items())
                    if HAS_RICH and console:
                        console.print(f"    [dim]{rx_str}[/]")
                    else:
                        click.echo(f"    {rx_str}")

        if HAS_RICH and console:
            console.print()
        else:
            click.echo()


def _inbox_display_threads(messages: list, my_identity: str) -> None:
    """Display a one-line summary per conversation peer.

    Each line shows: [N msgs] Name - "last message preview" - Xmin ago
    Sorted by most-recent message first.
    """
    peer_groups: dict = {}
    for msg in sorted(messages, key=lambda m: str(m.get("timestamp", ""))):
        sender = msg.get("sender") or ""
        recipient = msg.get("recipient") or ""
        peer = recipient if sender == my_identity else sender
        peer_groups.setdefault(peer, []).append(msg)

    sorted_peers = sorted(
        peer_groups.items(),
        key=lambda kv: str(kv[1][-1].get("timestamp", "")),
        reverse=True,
    )
    for peer, msgs in sorted_peers:
        name = _display_name(peer)
        count = len(msgs)
        last_content = msgs[-1].get("content", "")
        preview = last_content[:40] + ("…" if len(last_content) > 40 else "")
        ago = _ts_ago(msgs[-1].get("timestamp", ""))
        plural = "s" if count != 1 else ""
        if HAS_RICH and console:
            console.print(
                f"[dim][{count} msg{plural}][/] [cyan]{name}[/]"
                f" - [dim]\"{preview}\"[/] - [dim]{ago}[/]"
            )
        else:
            click.echo(f"[{count} msg{plural}] {name} - \"{preview}\" - {ago}")


@main.command()
@click.option("--limit", "-n", default=20, help="Max messages to show (default: 20).")
@click.option("--thread", "-t", default=None, help="Filter by thread ID.")
@click.option(
    "--since",
    "-s",
    default=None,
    type=int,
    metavar="MINUTES",
    help="Show messages from the last N minutes.",
)
@click.option(
    "--watch",
    "-w",
    is_flag=True,
    default=False,
    help="Continuously poll SKComm for new messages (Ctrl+C to stop).",
)
@click.option(
    "--interval",
    "-i",
    type=float,
    default=5.0,
    help="Poll interval in seconds for --watch mode (default: 5).",
)
@click.option(
    "--threads",
    "-T",
    is_flag=True,
    default=False,
    help="Show one line per conversation (thread list view).",
)
@click.option(
    "--unread",
    "-u",
    is_flag=True,
    default=False,
    help="Only show messages since last read (per ~/.skchat/read-state.json).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON for scripting.",
)
@click.option(
    "--from",
    "from_peer",
    default=None,
    metavar="PEER",
    help="Filter messages from a specific sender (name or identity URI).",
)
def inbox(
    limit: int,
    thread: Optional[str],
    since: Optional[int],
    watch: bool,
    interval: float,
    threads: bool,
    unread: bool,
    as_json: bool,
    from_peer: Optional[str],
) -> None:
    """Show recent incoming messages.

    Displays messages grouped by conversation peer with coloured
    dividers.  Your messages appear in blue; peer colours vary by name.

    With --watch, continuously polls SKComm for new messages and shows
    a Rich Live auto-updating table.  Press Ctrl+C to stop.

    With --threads, shows a one-line summary per conversation.

    With --unread, shows only messages since the last inbox view
    (state stored in ~/.skchat/read-state.json).

    With --json, outputs raw JSON for scripting.

    With --since N, shows only messages from the last N minutes.

    Examples:

        skchat inbox

        skchat inbox --limit 5

        skchat inbox --threads

        skchat inbox --unread

        skchat inbox --json | jq .

        skchat inbox --thread abc123

        skchat inbox --since 30

        skchat inbox --watch

        skchat inbox --from lumina

        skchat inbox --unread --from capauth:chef@skworld.io
    """
    if watch:
        _watch_inbox(interval=interval, limit=limit)
        return

    history = _get_history()

    if since is not None:
        messages = history.get_messages_since(minutes=since, limit=limit)
        # get_messages_since returns oldest-first; reverse for display
        messages = list(reversed(messages))
    elif thread:
        messages = history.get_thread_messages(thread, limit=limit)
    else:
        raw = history.load(limit=limit)
        messages = [
            {
                "sender": m.sender,
                "recipient": m.recipient,
                "content": m.content,
                "thread_id": m.thread_id,
                "timestamp": m.timestamp,
            }
            for m in raw
        ]

    # --from: filter by sender (substring match on name or full URI)
    if from_peer:
        fp_lower = from_peer.lower()
        messages = [
            m for m in messages
            if fp_lower in m.get("sender", "").lower()
        ]

    # --json: raw output for scripting (handles empty case too)
    if as_json:
        import json as _json
        click.echo(_json.dumps(messages, default=str, indent=2))
        return

    _print("")
    if not messages:
        if since is not None:
            _print(f"  [dim]No messages in the last {since} minute(s).[/]")
        else:
            _print("  [dim]No messages found.[/]")
        _print("")
        return

    my_identity = _get_identity()

    # --unread: filter to messages newer than the global last-read marker
    if unread:
        read_state = _load_read_state()
        last_read = read_state.get("_global", "")
        if last_read:
            messages = [
                m for m in messages
                if str(m.get("timestamp", "")) > last_read
            ]
        if not messages:
            _print("  [dim]No unread messages.[/]")
            _print("")
            return

    # Render
    if threads:
        _inbox_display_threads(messages, my_identity)
    else:
        _inbox_display_grouped(messages, my_identity)

    # Update global last-read marker so next --unread shows only newer msgs
    if messages:
        latest = max(str(m.get("timestamp", "")) for m in messages)
        rs = _load_read_state()
        rs["_global"] = latest
        _save_read_state(rs)

    _print("")


def _watch_inbox(interval: float = 5.0, limit: int = 50) -> None:
    """Continuously poll SKComm for new messages and display via Rich Live.

    Polls the transport on each tick, decrypts via ChatCrypto (if the
    transport has a crypto backend), stores every received message in
    ChatHistory, and renders an auto-updating Rich Live table.

    Args:
        interval: Seconds between each poll.
        limit: Maximum rows shown in the live table.
    """
    import time

    transport = _get_transport()

    if transport is None:
        _print(
            "\n  [yellow]No transport available.[/] Configure SKComm first.\n"
            "  Running in local-history-only mode — showing stored messages.\n"
        )

    history = _get_history()
    all_messages: list[dict] = []
    total_received = 0
    last_error: Optional[str] = None
    seen_ids: set = set()
    recent_notes: list[str] = []  # last few banner notifications for Live footer

    # Pre-populate from local history so the table isn't empty on start
    try:
        stored = history._store.list_memories(tags=["skchat:message"], limit=limit)
        all_messages = [
            history._memory_to_chat_dict(m)
            for m in stored
            if "skchat:message" in m.tags
        ]
        all_messages.sort(key=lambda d: str(d.get("timestamp", "")))
        all_messages = all_messages[-limit:]
    except Exception:
        pass

    def _build_live_table() -> "Panel":
        from rich.console import Group as RichGroup

        now = datetime.now(timezone.utc).strftime("%H:%M:%S")

        tbl = Table(
            show_header=True,
            header_style="bold cyan",
            box=None,
            padding=(0, 1),
            expand=True,
        )
        tbl.add_column("#", style="dim", max_width=4, no_wrap=True)
        tbl.add_column("From", style="cyan", max_width=28, no_wrap=True)
        tbl.add_column("Message", ratio=1)
        tbl.add_column("Thread", style="dim", max_width=12, no_wrap=True)
        tbl.add_column("Time", style="dim", max_width=8, no_wrap=True)

        display = all_messages[-limit:]
        if display:
            for idx, msg in enumerate(display, start=1):
                sender = msg.get("sender") or ""
                sender_label = _display_name(sender) if sender else "?"
                content = msg.get("content", "")
                preview = content[:60] + ("…" if len(content) > 60 else "")
                tid = (msg.get("thread_id") or "")[:12]
                ts = msg.get("timestamp", "")
                if hasattr(ts, "strftime"):
                    ts_str = ts.strftime("%H:%M:%S")
                elif isinstance(ts, str) and len(ts) >= 19:
                    ts_str = ts[11:19]
                else:
                    ts_str = str(ts)[:8]
                tbl.add_row(str(idx), sender_label, preview, tid, ts_str)
        else:
            tbl.add_row("", "[dim]No messages yet — waiting…[/]", "", "", "")

        status_parts = [f"Received this session: {total_received}"]
        if transport is None:
            status_parts.append("[yellow]no transport — local only[/]")
        if last_error:
            status_parts.append(f"[red]last error: {last_error[:60]}[/]")
        status_parts.append(f"Last poll: {now}")
        status_parts.append("Ctrl+C to stop")
        footer = "  [dim]" + " | ".join(status_parts) + "[/]"

        if recent_notes:
            notes_text = "\n".join(
                f"  [bold green]▶ {n}[/]" for n in recent_notes[-3:]
            )
            return Panel(
                RichGroup(tbl, notes_text, footer),
                title="[bold cyan]SKChat Live Inbox — Watch Mode[/]",
                border_style="cyan",
            )

        return Panel(
            RichGroup(tbl, footer),
            title="[bold cyan]SKChat Live Inbox — Watch Mode[/]",
            border_style="cyan",
        )

    if not HAS_RICH:
        # Plain-text fallback
        _print(f"Watching for messages every {interval}s (Ctrl+C to stop)…")
        seen_plain: set = set()
        try:
            while True:
                if transport is not None:
                    try:
                        new_msgs = transport.poll_inbox()
                        for m in new_msgs:
                            msg_key = getattr(m, "id", None) or f"{m.sender}:{m.content[:30]}"
                            if msg_key in seen_plain:
                                continue
                            seen_plain.add(msg_key)
                            total_received += 1
                            # Bell + banner for new message
                            sys.stdout.write("\a")
                            sys.stdout.flush()
                            _print(f"\n  *** New message from {m.sender}: {m.content[:80]} ***\n")
                    except Exception as exc:
                        _print(f"  [poll error: {exc}]")
                time.sleep(interval)
        except KeyboardInterrupt:
            _print(f"\nStopped. {total_received} message(s) received.\n")
        return

    try:
        from rich.live import Live

        with Live(
            _build_live_table(),
            console=console,
            refresh_per_second=1,
            screen=False,
        ) as live:
            try:
                while True:
                    if transport is not None:
                        try:
                            new_msgs = transport.poll_inbox()
                            if new_msgs:
                                for m in new_msgs:
                                    msg_key = getattr(m, "id", None) or f"{m.sender}:{m.content[:30]}"
                                    if msg_key in seen_ids:
                                        continue
                                    seen_ids.add(msg_key)
                                    total_received += 1
                                    # Bell + banner
                                    sys.stderr.write("\a")
                                    sys.stderr.flush()
                                    note = f"{m.sender}: {m.content[:60]}"
                                    recent_notes.append(note)
                                    if len(recent_notes) > 5:
                                        recent_notes.pop(0)
                                    all_messages.append(
                                        {
                                            "sender": m.sender,
                                            "content": m.content,
                                            "thread_id": m.thread_id,
                                            "timestamp": m.timestamp,
                                        }
                                    )
                                # Keep bounded
                                if len(all_messages) > limit * 2:
                                    all_messages[:] = all_messages[-limit:]
                                last_error = None
                        except Exception as exc:
                            last_error = str(exc)

                    live.update(_build_live_table())
                    time.sleep(interval)
            except KeyboardInterrupt:
                pass

    except ImportError:
        _print("  [yellow]Rich Live not available.[/]\n")
        return

    _print(
        f"\n  [dim]Watch stopped. {total_received} message(s) received this session.[/]\n"
    )


@main.command()
@click.argument("participant", required=False, default=None)
@click.option("--limit", "-n", default=20, help="Max messages to show (default: 20).")
def history(participant: Optional[str], limit: int) -> None:
    """Show conversation history with a peer, or all recent messages.

    Displays the message exchange between you and the specified
    participant, sorted newest first. If no participant is given,
    shows the most recent messages across all conversations.

    The participant can be either a full capauth URI or a friendly peer name
    that will be resolved from the peer registry.

    Examples:

        skchat history

        skchat history capauth:bob@skworld.io

        skchat history lumina --limit 10

        skchat history jarvis
    """
    identity = _get_identity()
    chat_history = _get_history()

    if participant is None:
        all_mems = chat_history._store.list_memories(
            tags=["skchat:message"],
            limit=limit,
        )
        messages = [
            chat_history._memory_to_chat_dict(m)
            for m in all_mems
            if "skchat:message" in m.tags
        ]
        messages.sort(key=lambda d: str(d.get("timestamp", "")), reverse=True)
        messages = messages[:limit]
        header = "Recent Messages"
    else:
        try:
            resolved_participant = resolve_peer_name(participant)
        except PeerResolutionError:
            resolved_participant = participant

        messages = chat_history.get_conversation(identity, resolved_participant, limit=limit)
        display_participant = (
            participant if participant == resolved_participant
            else f"{participant} ({resolved_participant})"
        )
        header = f"Conversation with {display_participant}"

    _print("")
    if not messages:
        if participant:
            _print(f"  [dim]No conversation history with {participant}.[/]")
        else:
            _print("  [dim]No messages found.[/]")
        _print("")
        return

    if HAS_RICH and console:
        console.print(Panel(
            f"[bold cyan]{header}[/]\n"
            f"[dim]{len(messages)} message{'s' if len(messages) != 1 else ''}[/]",
            border_style="cyan",
        ))

        for msg in reversed(messages):
            sender = msg.get("sender", "unknown")
            content = msg.get("content", "")
            ts = msg.get("timestamp", "")
            if isinstance(ts, str) and len(ts) > 19:
                ts = ts[:19]

            is_self = sender == identity
            style = "green" if is_self else "cyan"
            label = "You" if is_self else sender
            console.print(f"  [{style}]{label}[/] [dim]{ts}[/]")
            console.print(f"    {content}")
            console.print()
    else:
        for msg in reversed(messages):
            sender = msg.get("sender", "unknown")
            content = msg.get("content", "")[:80]
            _print(f"  {sender}: {content}")

    _print("")


@main.command()
@click.option("--limit", "-n", default=20, help="Max threads to show (default: 20).")
def threads(limit: int) -> None:
    """List all chat threads.

    Shows thread titles, participants, and message counts.

    Examples:

        skchat threads

        skchat threads --limit 5
    """
    chat_history = _get_history()
    thread_list = chat_history.list_threads(limit=limit)

    _print("")
    if not thread_list:
        _print("  [dim]No threads found.[/]")
        _print("")
        return

    if HAS_RICH and console:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"Threads ({len(thread_list)})",
        )
        table.add_column("ID", style="dim", max_width=12)
        table.add_column("Title", style="bold", max_width=30)
        table.add_column("Participants", style="cyan", max_width=40)
        table.add_column("Messages", justify="right")

        for t in thread_list:
            tid = (t.get("thread_id") or "")[:12]
            title = t.get("title", "Untitled")
            participants = ", ".join(t.get("participants", []))
            if len(participants) > 40:
                participants = participants[:37] + "..."
            count = str(t.get("message_count", 0))
            table.add_row(tid, title, participants, count)

        console.print(table)
    else:
        for t in thread_list:
            title = t.get("title", "Untitled")
            count = t.get("message_count", 0)
            _print(f"  {title} ({count} messages)")

    _print("")


@main.command()
@click.argument("query")
@click.option("--limit", "-n", default=10, help="Max results (default: 10).")
@click.option("--peer", default=None, help="Filter by sender or recipient peer URI.")
@click.option(
    "--after",
    "after_date",
    default=None,
    metavar="DATE",
    help="Show messages after this date (YYYY-MM-DD).",
)
@click.option(
    "--before",
    "before_date",
    default=None,
    metavar="DATE",
    help="Show messages before this date (YYYY-MM-DD).",
)
def search(
    query: str,
    limit: int,
    peer: Optional[str],
    after_date: Optional[str],
    before_date: Optional[str],
) -> None:
    """Search chat messages by content.

    Full-text search across all stored messages.
    Results are shown in a table: Date | From | Preview (60 chars).
    Matching text is highlighted in bold yellow.

    Examples:

        skchat search "quantum upgrade"

        skchat search "deploy" --limit 5

        skchat search "hello lumina" --limit 20

        skchat search "meeting" --peer capauth:bob@test --after 2026-01-01
    """
    # Parse optional date filters
    after_dt: Optional[datetime] = None
    before_dt: Optional[datetime] = None
    if after_date:
        try:
            after_dt = datetime.fromisoformat(after_date).replace(tzinfo=timezone.utc)
        except ValueError:
            click.echo(f"  Error: --after '{after_date}' is not a valid date (YYYY-MM-DD).", err=True)
            raise SystemExit(1)
    if before_date:
        try:
            before_dt = datetime.fromisoformat(before_date).replace(tzinfo=timezone.utc)
        except ValueError:
            click.echo(f"  Error: --before '{before_date}' is not a valid date (YYYY-MM-DD).", err=True)
            raise SystemExit(1)

    # Fetch more when filtering so we can trim to --limit after
    fetch_limit = limit * 4 if (peer or after_dt or before_dt) else limit
    chat_history = _get_history()
    results = chat_history.search_messages(query, limit=fetch_limit)

    # Peer filter
    if peer:
        results = [
            m for m in results
            if peer in (m.get("sender") or "") or peer in (m.get("recipient") or "")
        ]

    # Date filters
    if after_dt or before_dt:
        def _to_aware(ts: object) -> Optional[datetime]:
            if ts is None:
                return None
            if isinstance(ts, datetime):
                return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            if isinstance(ts, str):
                try:
                    dt = datetime.fromisoformat(ts)
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    return None
            return None

        filtered = []
        for m in results:
            ts_dt = _to_aware(m.get("timestamp"))
            if ts_dt is None:
                continue
            if after_dt and ts_dt < after_dt:
                continue
            if before_dt and ts_dt > before_dt:
                continue
            filtered.append(m)
        results = filtered

    results = results[:limit]

    _print("")
    if not results:
        _print(f"  [dim]No messages matching '{query}'.[/]")
        _print("")
        return

    def _fmt_search_ts(ts: object) -> str:
        """Compact timestamp: today → HH:MM, same year → MM-DD HH:MM, else YY-MM-DD."""
        try:
            if isinstance(ts, str):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif isinstance(ts, datetime):
                dt = ts
            else:
                return str(ts)[:16]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if dt.date() == now.date():
                return dt.strftime("%H:%M")
            if dt.year == now.year:
                return dt.strftime("%m-%d %H:%M")
            return dt.strftime("%y-%m-%d %H:%M")
        except Exception:
            return str(ts)[:16]

    def _short_uri(uri: str) -> str:
        """Convert a full identity URI to a short display name."""
        u = uri.replace("capauth:", "").replace("nostr:", "").replace("group:", "grp/")
        return u.split("@")[0][:18] if "@" in u else u[:18]

    def _rich_highlight(text: str, q: str) -> "Text":
        """Return a Rich Text object with query terms highlighted bold yellow."""
        from rich.text import Text as RText
        t = RText()
        if not q:
            t.append(text)
            return t
        pattern = re.compile(re.escape(q), re.IGNORECASE)
        last = 0
        for m in pattern.finditer(text):
            t.append(text[last:m.start()])
            t.append(m.group(), style="bold yellow")
            last = m.end()
        t.append(text[last:])
        return t

    n_results = len(results)
    title_str = f"Search: '{query}'  {n_results} result{'s' if n_results != 1 else ''}"

    if HAS_RICH and console:
        from rich.box import SIMPLE_HEAD
        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=SIMPLE_HEAD,
            padding=(0, 1),
            title=title_str,
        )
        table.add_column("When", style="dim", max_width=13, no_wrap=True)
        table.add_column("From", style="cyan", max_width=18, no_wrap=True)
        table.add_column("To", style="dim", max_width=18, no_wrap=True)
        table.add_column("Preview", max_width=50)

        for msg in results:
            sender = _short_uri(msg.get("sender") or "?")
            recip  = _short_uri(msg.get("recipient") or "")
            content = str(msg.get("content") or "").replace("\n", " ")
            preview_text = content[:60] + ("\u2026" if len(content) > 60 else "")
            preview_rich = _rich_highlight(preview_text, query)
            ts_str = _fmt_search_ts(msg.get("timestamp", ""))
            table.add_row(ts_str, sender, recip, preview_rich)

        console.print(table)
    else:
        click.echo(f"\n  {title_str}\n")
        click.echo(f"  {'When':<14}  {'From':<18}  {'To':<18}  Preview")
        click.echo("  " + "-" * 80)
        for msg in results:
            sender = _short_uri(msg.get("sender") or "?")
            recip  = _short_uri(msg.get("recipient") or "")
            content = str(msg.get("content") or "").replace("\n", " ")
            raw_preview = content[:50] + ("\u2026" if len(content) > 50 else "")
            preview = _highlight_query(raw_preview, query)
            ts_str = _fmt_search_ts(msg.get("timestamp", ""))
            click.echo(f"  {ts_str:<14}  {sender:<18}  {recip:<18}  {preview}")

    _print("")


@main.command()
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "markdown", "txt"], case_sensitive=False),
    default="json",
    help="Export format: json, markdown, or txt (default: json).",
)
@click.option(
    "--output",
    "-o",
    default=None,
    help="Output file path. Defaults to ~/.skchat/export-{timestamp}.{ext}.",
)
@click.option("--peer", default=None, help="Filter by conversation partner URI.")
def export(fmt: str, output: Optional[str], peer: Optional[str]) -> None:
    """Export chat history to a file.

    Exports all stored messages in the chosen format.
    Use --peer to limit export to one conversation.

    JSON: array of {sender, content, timestamp, thread_id}

    Markdown: # Chat Export / ## thread_id / **sender**: content

    Examples:

        skchat export

        skchat export --format markdown --peer lumina

        skchat export --format json --output ~/backup.json

        skchat export --format txt --peer capauth:bob@test
    """
    import json as _json

    chat_history = _get_history()

    if peer:
        try:
            resolved_peer = resolve_peer_name(peer)
        except PeerResolutionError:
            resolved_peer = peer
        identity = _get_identity()
        messages = chat_history.get_conversation(identity, resolved_peer, limit=10000)
    else:
        all_mems = chat_history._store.list_memories(tags=["skchat:message"], limit=10000)
        messages = [
            chat_history._memory_to_chat_dict(m)
            for m in all_mems
            if "skchat:message" in m.tags
        ]
        messages.sort(key=lambda d: str(d.get("timestamp", "")))

    # Determine output path
    if output is None:
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        ext = "md" if fmt == "markdown" else fmt
        out_path = Path(SKCHAT_HOME).expanduser() / f"export-{ts_str}.{ext}"
    else:
        out_path = Path(output).expanduser()

    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _fmt_ts(ts: object) -> str:
        if ts is None:
            return ""
        if isinstance(ts, datetime):
            return ts.isoformat()
        return str(ts)

    if fmt == "json":
        records = [
            {
                "sender": m.get("sender", ""),
                "content": m.get("content", ""),
                "timestamp": _fmt_ts(m.get("timestamp")),
                "thread_id": m.get("thread_id") or "",
            }
            for m in messages
        ]
        file_content = _json.dumps(records, indent=2, ensure_ascii=False)

    elif fmt == "markdown":
        lines = ["# Chat Export\n"]
        threads: dict = {}
        no_thread = []
        for m in messages:
            tid = m.get("thread_id") or ""
            if tid:
                threads.setdefault(tid, []).append(m)
            else:
                no_thread.append(m)
        for tid, msgs in threads.items():
            lines.append(f"\n## {tid}\n")
            for m in msgs:
                sender = m.get("sender", "unknown")
                msg_content = m.get("content", "")
                lines.append(f"**{sender}**: {msg_content}\n")
        if no_thread:
            lines.append("\n## (no thread)\n")
            for m in no_thread:
                sender = m.get("sender", "unknown")
                msg_content = m.get("content", "")
                lines.append(f"**{sender}**: {msg_content}\n")
        file_content = "\n".join(lines)

    else:  # txt
        lines = []
        for m in messages:
            sender = m.get("sender", "unknown")
            msg_content = m.get("content", "")
            ts = _fmt_ts(m.get("timestamp"))
            lines.append(f"[{ts}] {sender}: {msg_content}")
        file_content = "\n".join(lines)

    out_path.write_text(file_content, encoding="utf-8")

    _print("")
    if HAS_RICH and console:
        console.print(Panel(
            f"[bold]Format:[/]   {fmt}\n"
            f"[bold]Messages:[/] {len(messages)}\n"
            f"[bold]Output:[/]   [cyan]{out_path}[/]",
            title="Export Complete",
            border_style="green",
        ))
    else:
        _print(f"  Exported {len(messages)} message(s) ({fmt}) → {out_path}")
    _print("")


@main.command()
@click.option("--to", "peer", default="lumina", show_default=True, help="Recipient peer name or capauth URI.")
@click.option(
    "--interval",
    "-i",
    type=float,
    default=2.0,
    show_default=True,
    help="Poll interval in seconds.",
)
@click.option("--thread", "-t", default=None, help="Thread ID for this conversation.")
@click.option("--group", is_flag=True, default=False, help="Address a group conversation instead of a peer.")
def chat(peer: str, interval: float, thread: Optional[str], group: bool) -> None:
    """Open an interactive chat session with a peer.

    Polls for new messages every INTERVAL seconds and displays them inline.
    Type a message and press Enter to send. Ctrl+C exits.

    Examples:

        skchat chat

        skchat chat --to lumina

        skchat chat --to capauth:chef@skworld.io

        skchat chat --to lumina --thread proj-alpha
    """
    import threading
    import time

    try:
        import readline as _readline
        _HAS_READLINE = True
    except ImportError:
        _readline = None  # type: ignore[assignment]
        _HAS_READLINE = False

    from .presence import PresenceIndicator, PresenceState

    try:
        peer_uri = resolve_peer_name(peer)
    except PeerResolutionError:
        peer_uri = peer

    peer_display = _display_name(peer_uri)
    peer_label = peer if peer == peer_uri else f"{peer} ({peer_uri})"
    identity = _get_identity()
    messenger = AgentMessenger.from_identity(identity=identity)

    # ── Presence broadcasting ────────────────────────────────────────────────
    def _send_presence(state: PresenceState) -> None:
        """Fire-and-forget presence signal to the peer via SKComm or file."""
        indicator = PresenceIndicator(
            identity_uri=identity,
            state=state,
            thread_id=thread,
        )
        payload = indicator.model_dump_json()
        try:
            from skcomm.models import MessageType
            xport = messenger._transport
            if xport is not None and hasattr(xport, "_skcomm"):
                xport._skcomm.send(
                    recipient=peer_uri,
                    message=payload,
                    message_type=MessageType.HEARTBEAT,
                )
                return
        except Exception:
            pass
        # File-transport fallback: drop JSON in shared inbox dir
        try:
            import uuid as _uuid
            inbox_dir = Path("~/.skchat/inbox").expanduser()
            inbox_dir.mkdir(parents=True, exist_ok=True)
            (inbox_dir / f"presence-{_uuid.uuid4().hex[:8]}.json").write_text(payload)
        except Exception:
            pass

    # Throttle: at most 1 TYPING broadcast per 2 s using threading.Timer
    _TYPING_THROTTLE = 2.0
    _typing_timer: list[Optional[threading.Timer]] = [None]
    _typing_lock = threading.Lock()

    def _clear_typing_throttle() -> None:
        with _typing_lock:
            _typing_timer[0] = None

    def _on_typing() -> None:
        with _typing_lock:
            if _typing_timer[0] is not None:
                return  # throttled: a broadcast was sent recently
            t = threading.Timer(_TYPING_THROTTLE, _clear_typing_throttle)
            t.daemon = True
            t.start()
            _typing_timer[0] = t
        threading.Thread(
            target=_send_presence, args=(PresenceState.TYPING,), daemon=True
        ).start()

    # Readline-buffer polling thread: detects when the user starts typing
    _stop_poller = threading.Event()
    _prev_buf: list[str] = [""]

    def _poll_readline() -> None:
        while not _stop_poller.is_set():
            if _HAS_READLINE:
                try:
                    buf = _readline.get_line_buffer()
                    if buf and buf != _prev_buf[0]:
                        _prev_buf[0] = buf
                        _on_typing()
                except Exception:
                    pass
            _stop_poller.wait(0.3)

    _poller = threading.Thread(target=_poll_readline, daemon=True)
    _poller.start()

    _print("")
    if HAS_RICH and console:
        console.print(Panel(
            f"[bold]Peer:[/]     [cyan]{peer_label}[/]\n"
            f"[bold]You:[/]      [blue]{_display_name(identity)}[/]\n"
            f"[dim]Polling every {interval}s · type + Enter to send · Ctrl+C to quit[/]",
            title="[bold cyan]SKChat Interactive[/]",
            border_style="cyan",
        ))
    else:
        click.echo(f"  Chat with {peer_label}  (Ctrl+C to exit)\n")

    # Seed seen-set from local history to avoid re-printing old messages
    history = _get_history()
    recent = history.get_conversation(identity, peer_uri, limit=10)
    seen_keys: set = set()

    if recent:
        if HAS_RICH and console:
            console.print("[dim]\u2500\u2500\u2500 recent \u2500\u2500\u2500[/]")
        else:
            click.echo("\u2500\u2500\u2500 recent \u2500\u2500\u2500")

        for msg in reversed(recent):
            sender = msg.get("sender", "unknown")
            content = msg.get("content", "")
            ts_str = _ts_hhmm(msg.get("timestamp", ""))
            is_me = sender == identity
            label = "You" if is_me else peer_display
            color = "blue" if is_me else "green"
            if HAS_RICH and console:
                console.print(f"  [dim]{ts_str}[/] [{color}]{label}:[/] {content}")
            else:
                click.echo(f"  [{ts_str}] {label}: {content}")
            seen_keys.add(_msg_key(msg))

        if HAS_RICH and console:
            console.print("[dim]\u2500\u2500\u2500 live \u2500\u2500\u2500[/]\n")
        else:
            click.echo("\u2500\u2500\u2500 live \u2500\u2500\u2500\n")

    # Announce ONLINE presence on session start
    threading.Thread(target=_send_presence, args=(PresenceState.ONLINE,), daemon=True).start()

    # Build the REPL prompt string (colored via ANSI when Rich is available)
    _you_label = _display_name(identity)
    if HAS_RICH:
        _prompt_text = f"\033[34m[{_you_label}]\033[0m >> "
    else:
        _prompt_text = f"[{_you_label}] >> "

    stop_event = threading.Event()

    def _poll_loop() -> None:
        while not stop_event.is_set():
            try:
                msgs = messenger.receive(limit=50)
                for msg in reversed(msgs):
                    if msg.get("sender") != peer_uri:
                        continue
                    key = _msg_key(msg)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    content = msg.get("content", "")
                    ts_str = _ts_hhmm(msg.get("timestamp", ""))
                    # Clear current input line, print message, re-show prompt
                    sys.stdout.write("\r")
                    if HAS_RICH and console:
                        console.print(
                            f"  [dim]{ts_str}[/] [green]{peer_display}:[/] {content}"
                        )
                    else:
                        click.echo(f"  [{ts_str}] {peer_display}: {content}")
                    sys.stdout.write(_prompt_text)
                    sys.stdout.flush()
            except Exception:
                pass
            stop_event.wait(interval)

    poll_thread = threading.Thread(target=_poll_loop, daemon=True)
    poll_thread.start()

    try:
        while True:
            try:
                user_input = input(_prompt_text)
            except EOFError:
                break
            text = user_input.strip()
            if not text:
                continue
            _prev_buf[0] = ""  # reset buffer tracker so next typed char fires
            _transport = _get_chat_transport()
            if _transport is not None:
                result = _transport.send_and_store(peer_uri, text, thread_id=thread)
            else:
                _msg = ChatMessage(
                    sender=identity, recipient=peer_uri, content=text,
                    thread_id=thread, delivery_status=DeliveryStatus.PENDING,
                )
                history.save(_msg)
                result = {"delivered": False}
            delivered = result.get("delivered", False)
            ts_str = datetime.now(timezone.utc).strftime("%H:%M")
            if HAS_RICH and console:
                tag = " [dim](delivered)[/]" if delivered else " [dim](stored)[/]"
                console.print(f"  [dim]{ts_str}[/] [blue]You:[/] {text}{tag}")
            else:
                tag = " (delivered)" if delivered else " (stored)"
                click.echo(f"  [{ts_str}] You: {text}{tag}")
            # Signal ONLINE (done typing) after message is sent
            threading.Thread(
                target=_send_presence, args=(PresenceState.ONLINE,), daemon=True
            ).start()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        _stop_poller.set()
        _send_presence(PresenceState.OFFLINE)

    _print("\n  [dim]Chat session ended.[/]\n")


@main.command(name="receive")
def receive_cmd() -> None:
    """Poll SKComm transports for incoming chat messages.

    Checks all configured transports for new messages addressed
    to this agent and stores them in local history.

    Examples:

        skchat receive
    """
    transport = _get_chat_transport()
    if transport is None:
        _print("")
        _print("  [yellow]No transports available.[/] Configure SKComm first.")
        _print("  See: skcomm init --name YourAgent --email you@example.com")
        _print("")
        return

    messages = transport.poll_inbox()

    _print("")
    if not messages:
        _print("  [dim]No new messages.[/]")
        _print("")
        return

    if HAS_RICH and console:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"Received ({len(messages)} message{'s' if len(messages) != 1 else ''})",
        )
        table.add_column("From", style="cyan", max_width=30)
        table.add_column("Content", max_width=50)
        table.add_column("Thread", style="dim", max_width=12)

        for msg in messages:
            preview = msg.content[:50] + ("..." if len(msg.content) > 50 else "")
            tid = (msg.thread_id or "")[:12]
            table.add_row(msg.sender, preview, tid)

        console.print(table)
        console.print(f"\n  [dim]Stored {len(messages)} message(s) in history.[/]")
    else:
        for msg in messages:
            _print(f"  {msg.sender}: {msg.content[:80]}")
        _print(f"  Stored {len(messages)} message(s) in history.")

    _print("")


@main.command()
@click.option("--interval", "-i", type=float, default=3.0, help="Poll interval in seconds (default: 3).")
@click.option("--limit", "-n", default=20, help="Max messages to show per poll.")
@click.option(
    "--notify",
    is_flag=True,
    default=False,
    help="Send a desktop notification (via notify-send) for each new message.",
)
@click.option(
    "--sound",
    is_flag=True,
    default=False,
    help="Play a ping sound (via paplay/aplay) for each new message.",
)
@click.option(
    "--speak",
    is_flag=True,
    default=False,
    help="Read new messages aloud via Piper TTS (local, sovereign).",
)
@click.option(
    "--group",
    default=None,
    metavar="GROUP_ID",
    help="Watch only messages from a specific group (by group ID).",
)
@click.option(
    "--all",
    "watch_all",
    is_flag=True,
    default=False,
    help="Watch all threads including group messages.",
)
def watch(
    interval: float,
    limit: int,
    notify: bool,
    sound: bool,
    speak: bool,
    group: Optional[str],
    watch_all: bool,
) -> None:
    """Watch for incoming messages in real-time.

    Continuously polls SKComm for new messages and displays
    them as they arrive with color. Press Ctrl+C to stop.

    Use --group GROUP_ID to watch a specific group, --all to include all
    threads, --notify for desktop notifications, --sound for audio alerts,
    and --speak to have messages read aloud via Piper TTS.

    Examples:

        skchat watch

        skchat watch --interval 2

        skchat watch --group d4f3281e-fa92-474c-a8cd-f0a2a4c31c33

        skchat watch --all --notify

        skchat watch --speak
    """
    import time

    transport = _get_transport()
    if transport is None:
        _print("\n  [yellow]No transport available.[/] Configure SKComm first.\n")
        return

    identity = _get_identity()
    group_label = group if group else ("all threads" if watch_all else "skworld-team")

    # Header
    header = (
        click.style("SKChat LIVE", fg="cyan", bold=True)
        + click.style(" | ", dim=True)
        + click.style(identity, fg="white")
        + click.style(" | group: ", dim=True)
        + click.style(group_label, fg="yellow")
    )
    click.echo(f"\n  {header}")
    click.echo(click.style(f"  Polling every {interval}s — Ctrl+C to stop\n", dim=True))

    total_received = 0
    start_time = time.monotonic()

    def _handle_side_effects(msg: object) -> None:
        sender = getattr(msg, "sender", "unknown")
        content = getattr(msg, "content", "")
        if sender == identity:
            return
        sender_short = sender.split(":")[-1] if ":" in sender else sender
        preview = content[:80]
        if notify:
            _notify(sender_short, preview)
        if sound:
            _play_sound()
        if speak:
            _speak_message(sender, sender_short, preview)

    def _matches_filter(msg: object) -> bool:
        """Return True if this message should be shown given the current flags."""
        thread_id = getattr(msg, "thread_id", None)
        if group:
            return thread_id == group
        if watch_all:
            return True
        # Default: show everything (DMs have no thread_id, group msgs have one)
        return True

    if HAS_RICH and console:
        try:
            from rich.live import Live

            table = _build_watch_table([], total_received)

            with Live(table, console=console, refresh_per_second=0.5) as live:
                last_heartbeat = time.monotonic()
                while True:
                    try:
                        raw = transport.poll_inbox()
                        messages = [m for m in raw if _matches_filter(m)] if raw else []
                        if messages:
                            total_received += len(messages)
                            for m in messages:
                                _handle_side_effects(m)
                            table = _build_watch_table(messages, total_received)
                            live.update(table)
                            last_heartbeat = time.monotonic()
                        else:
                            if time.monotonic() - last_heartbeat >= 10:
                                last_heartbeat = time.monotonic()
                    except Exception as exc:
                        live.update(Panel(f"[red]Poll error: {exc}[/]", border_style="red"))
                    time.sleep(interval)
        except KeyboardInterrupt:
            pass
        except ImportError:
            _print("  [yellow]Rich Live not available. Use 'skchat receive' for one-shot poll.[/]\n")
    else:
        last_heartbeat = time.monotonic()
        try:
            while True:
                raw = transport.poll_inbox()
                messages = [m for m in raw if _matches_filter(m)] if raw else []
                if messages:
                    total_received += len(messages)
                    last_heartbeat = time.monotonic()
                    for msg in messages:
                        _handle_side_effects(msg)
                        sender = getattr(msg, "sender", "unknown")
                        content = getattr(msg, "content", "")
                        ts = getattr(msg, "timestamp", None)
                        ts_str = (
                            ts.strftime("%H:%M:%S")
                            if ts and hasattr(ts, "strftime")
                            else datetime.now(timezone.utc).strftime("%H:%M:%S")
                        )
                        click.echo(_format_watch_line(sender, content, ts_str, identity))
                else:
                    if time.monotonic() - last_heartbeat >= 10:
                        click.echo(".", nl=False)
                        sys.stdout.flush()
                        last_heartbeat = time.monotonic()
                time.sleep(interval)
        except KeyboardInterrupt:
            pass

    uptime_secs = int(time.monotonic() - start_time)
    uptime_str = f"{uptime_secs // 60}m {uptime_secs % 60}s"
    click.echo(
        f"\n\n  {click.style('Stopped.', dim=True)}"
        f" {click.style(str(total_received), fg='cyan', bold=True)} messages seen"
        f" | uptime: {click.style(uptime_str, dim=True)}\n"
    )


@main.command(name="send-file")
@click.argument("recipient")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
def send_file_cmd(recipient: str, file_path: Path) -> None:
    """Send a file to a recipient via SKComm.

    Chunks and AES-256-GCM encrypts the file, then sends
    FILE_TRANSFER_INIT, FILE_CHUNK (xN), and FILE_TRANSFER_DONE messages.
    Transfer metadata is persisted to ~/.skchat/transfers/.

    Examples:

        skchat send-file lumina ~/report.pdf

        skchat send-file capauth:bob@skworld.io /tmp/contract.zip
    """
    from .files import FileTransferService

    identity = _get_identity()
    try:
        resolved_recipient = resolve_peer_name(recipient)
    except PeerResolutionError:
        resolved_recipient = recipient

    skcomm = None
    try:
        from skcomm.core import SKComm
        skcomm = SKComm.from_config()
    except Exception:
        pass

    service = FileTransferService(identity=identity, skcomm=skcomm)

    _print("")
    _print(f"  Sending [cyan]{file_path.name}[/] to [cyan]{resolved_recipient}[/] ...")

    try:
        transfer_id = service.send_file(resolved_recipient, file_path)
    except FileNotFoundError as exc:
        _print(f"\n  [red]Error:[/] {exc}\n")
        sys.exit(1)
    except Exception as exc:
        _print(f"\n  [red]Send failed:[/] {exc}\n")
        sys.exit(1)

    if HAS_RICH and console:
        console.print(
            f"  [green]Done.[/] Transfer ID: [dim]{transfer_id}[/]\n"
            f"  Track with: [cyan]skchat transfers[/]"
        )
    else:
        click.echo(f"  Done. Transfer ID: {transfer_id}")
    _print("")


@main.command(name="transfers")
def transfers_cmd() -> None:
    """List active and completed file transfers.

    Shows all tracked transfers from ~/.skchat/transfers/ with
    direction (OUT/IN), status, and progress.

    Examples:

        skchat transfers
    """
    from .files import FileTransferService

    service = FileTransferService(identity=_get_identity())
    transfers = service.list_transfers()

    _print("")
    if not transfers:
        _print("  [dim]No transfers found.[/]")
        _print("")
        return

    if HAS_RICH and console:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"File Transfers ({len(transfers)})",
        )
        table.add_column("ID", style="dim", max_width=14)
        table.add_column("File")
        table.add_column("Dir", max_width=4)
        table.add_column("Peer", max_width=26)
        table.add_column("Status")
        table.add_column("Progress", max_width=8)

        for t in transfers:
            tid = t.get("transfer_id", "")[:12]
            fname = t.get("filename", "")
            is_out = t.get("direction") == "outbound"
            direction = "OUT" if is_out else "IN"
            peer = (t.get("recipient") if is_out else t.get("sender")) or ""
            peer = peer[:26]
            status = t.get("status", "")
            pct = f"{int(t.get('progress', 0) * 100)}%"

            if status == "complete":
                status_str = f"[green]{status}[/]"
            elif status in ("sending", "receiving", "ready_to_assemble"):
                status_str = f"[yellow]{status}[/]"
            elif status == "failed":
                status_str = f"[red]{status}[/]"
            else:
                status_str = status

            table.add_row(tid, fname, direction, peer, status_str, pct)

        console.print(table)
    else:
        for t in transfers:
            click.echo(
                f"  {t.get('transfer_id', '')[:12]}  "
                f"{t.get('filename', '')}  "
                f"{'OUT' if t.get('direction') == 'outbound' else 'IN'}  "
                f"{t.get('status', '')}  "
                f"{int(t.get('progress', 0) * 100)}%"
            )

    _print("")


@main.command(name="receive-file")
@click.argument("transfer_id")
@click.option(
    "--output",
    "-o",
    default=None,
    metavar="DIR",
    help="Directory to save the assembled file (default: ~/.skchat/received/).",
)
def receive_file_cmd(transfer_id: str, output: Optional[str]) -> None:
    """Reassemble a received file transfer.

    Looks up the transfer by ID, checks that all chunks are present,
    reassembles the file, and verifies the SHA-256 digest.

    TRANSFER_ID is the UUID shown by `skchat transfers` or returned
    by `skchat send-file`.

    Examples:

        skchat receive-file abc12345-...

        skchat receive-file abc12345-... --output ~/Downloads
    """
    from .files import FileTransferService

    output_dir = Path(output).expanduser() if output else None
    service = FileTransferService(identity=_get_identity())

    _print("")
    _print(f"  Assembling transfer [dim]{transfer_id[:12]}[/] ...")

    try:
        out_path = service.receive_file(transfer_id, output_dir=output_dir)
    except Exception as exc:
        _print(f"\n  [red]Error:[/] {exc}\n")
        sys.exit(1)

    if out_path is None:
        _print(
            "  [red]Cannot assemble:[/] transfer not found or chunks incomplete.\n"
            "  Check incoming chunks with [cyan]skchat transfers[/]."
        )
        sys.exit(1)

    if HAS_RICH and console:
        console.print(f"  [green]Done.[/] Saved to [cyan]{out_path}[/]")
    else:
        click.echo(f"  Done. Saved to {out_path}")
    _print("")


_MENTION_RE = re.compile(r"(@\S+)")
_SOUND_PATHS = [
    "/usr/share/sounds/freedesktop/stereo/message.oga",
    "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga",
]


def _notify(sender_short: str, preview: str) -> None:
    """Fire a desktop notification via notify-send (best-effort)."""
    subprocess.run(
        [
            "notify-send",
            "--urgency=normal",
            "--icon=dialog-information",
            f"SKChat from {sender_short}",
            preview,
        ],
        capture_output=True,
    )


def _play_sound() -> None:
    """Play a notification ping via paplay/aplay (best-effort)."""
    for path in _SOUND_PATHS:
        if Path(path).exists():
            for player in ("paplay", "aplay"):
                result = subprocess.run(
                    [player, path],
                    capture_output=True,
                )
                if result.returncode == 0:
                    return
            return


_LUMINA_ID = "capauth:lumina@skworld.io"


def _speak_message(sender: str, sender_short: str, preview: str) -> None:
    """Read a message aloud via Piper TTS (best-effort, silently degrades)."""
    try:
        from .voice import VoicePlayer, LUMINA_VOICE, DEFAULT_VOICE

        voice = LUMINA_VOICE if sender == _LUMINA_ID else DEFAULT_VOICE
        player = VoicePlayer(voice=voice)
        player.speak(f"Message from {sender_short}: {preview}")
    except Exception:  # noqa: BLE001
        pass


def _highlight_mentions(text: str) -> str:
    """Wrap @mentions in click.style yellow."""
    parts = _MENTION_RE.split(text)
    out = []
    for part in parts:
        if _MENTION_RE.match(part):
            out.append(click.style(part, fg="yellow", bold=True))
        else:
            out.append(part)
    return "".join(out)


def _highlight_query(text: str, query: str) -> str:
    """Highlight query terms in text with bold yellow via click.style.

    Args:
        text: The text to search within.
        query: The search query whose terms to highlight.

    Returns:
        String with query matches wrapped in click.style bold yellow.
    """
    if not query:
        return text
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    parts = pattern.split(text)
    matches = pattern.findall(text)
    result = parts[0]
    for match, part in zip(matches, parts[1:]):
        result += click.style(match, bold=True, fg="yellow")
        result += part
    return result


def _format_watch_line(sender: str, content: str, ts_str: str, own_id: str) -> str:
    """Format a single watch message line with click.style colors.

    Format: ``[HH:MM:SS] sender_name: message body``

    Args:
        sender: Message sender identifier (full capauth URI or plain name).
        content: Message body.
        ts_str: Formatted timestamp string (HH:MM:SS).
        own_id: Local identity; own messages are dimmed.

    Returns:
        Formatted string ready for click.echo.
    """
    is_own = sender == own_id
    sender_name = sender.split(":")[-1] if ":" in sender else sender
    ts_col = click.style(f"[{ts_str}]", fg="white", dim=True)
    preview = content[:120] + ("…" if len(content) > 120 else "")
    if is_own:
        sender_col = click.style(sender_name, dim=True)
        body = click.style(preview, dim=True)
        return f"  {ts_col} {sender_col}: {body}"
    sender_col = click.style(sender_name, fg="cyan", bold=True)
    body = _highlight_mentions(preview)
    return f"  {ts_col} {sender_col}: {body}"


def _build_watch_table(recent_messages: list, total: int) -> "Panel":
    """Build a Rich table for the watch display.

    Args:
        recent_messages: Most recent batch of received messages.
        total: Total messages received since watch started.

    Returns:
        Panel: Rich panel with message table.
    """
    from rich.console import Group as RichGroup

    now = datetime.now(timezone.utc).strftime("%H:%M:%S")

    table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 2),
        expand=True,
    )
    table.add_column("From", style="cyan", max_width=25)
    table.add_column("Content", max_width=50)
    table.add_column("Thread", style="dim", max_width=12)
    table.add_column("Time", style="dim", max_width=10)

    if recent_messages:
        for msg in recent_messages:
            preview = msg.content[:50] + ("..." if len(msg.content) > 50 else "")
            tid = (msg.thread_id or "")[:12]
            ts = msg.timestamp.strftime("%H:%M:%S") if hasattr(msg, "timestamp") else ""
            table.add_row(msg.sender, preview, tid, ts)
    else:
        table.add_row("[dim]waiting...[/]", "", "", "")

    footer = f"[dim]Total received: {total} | Last poll: {now} | Ctrl+C to stop[/]"

    return Panel(
        RichGroup(table, "", footer),
        title="[bold cyan]SKChat Live Inbox[/]",
        border_style="cyan",
    )


@main.group()
def daemon() -> None:
    """Manage the SKChat receive daemon.

    The daemon polls SKComm transports in the background and
    stores incoming messages in local history automatically.
    PID is tracked at ~/.skchat/daemon.pid.

    Examples:

        skchat daemon start

        skchat daemon start --interval 10 --log-file ~/.skchat/daemon.log

        skchat daemon status

        skchat daemon stop
    """


@daemon.command("start")
@click.option("--interval", "-i", type=float, default=5.0, help="Poll interval in seconds (default: 5).")
@click.option("--log-file", "-l", default=None, help="Path to log file (default: ~/.skchat/daemon.log).")
@click.option("--quiet", "-q", is_flag=True, help="Suppress console output in daemon process.")
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (blocking, for debugging).")
def daemon_start(interval: float, log_file: Optional[str], quiet: bool, foreground: bool) -> None:
    """Start the receive daemon in the background.

    Spawns a background process that polls SKComm every INTERVAL
    seconds. PID is written to ~/.skchat/daemon.pid.

    Examples:

        skchat daemon start

        skchat daemon start --interval 10

        skchat daemon start --foreground
    """
    try:
        from .daemon import start_daemon, is_running, _read_pid

        if is_running():
            pid = _read_pid()
            _print(f"\n  [yellow]Daemon already running[/] (PID {pid})\n")
            return

        if foreground:
            _print(f"\n  [cyan]Starting daemon in foreground[/] (Ctrl+C to stop)...\n")
            from .daemon import run_daemon
            try:
                run_daemon(interval=interval, log_file=log_file, quiet=quiet)
            except KeyboardInterrupt:
                _print("\n  [dim]Daemon stopped.[/]\n")
            return

        pid = start_daemon(interval=interval, log_file=log_file, quiet=quiet, background=True)
        from .daemon import DAEMON_LOG_FILE
        log_path = Path(log_file).expanduser() if log_file else DAEMON_LOG_FILE.expanduser()

        _print(f"\n  [green]Daemon started[/] (PID {pid})")
        _print(f"  Poll interval: [cyan]{interval}s[/]")
        _print(f"  Log: [dim]{log_path}[/]")
        _print(f"  PID file: [dim]~/.skchat/daemon.pid[/]")
        _print(f"  Stop with: [cyan]skchat daemon stop[/]\n")

    except RuntimeError as exc:
        _print(f"\n  [red]Error:[/] {exc}\n")
        sys.exit(1)
    except Exception as exc:
        _print(f"\n  [red]Error:[/] {exc}\n")
        sys.exit(1)


@daemon.command("stop")
def daemon_stop() -> None:
    """Stop the running daemon.

    Sends SIGTERM to the daemon process and removes the PID file.

    Examples:

        skchat daemon stop
    """
    try:
        from .daemon import stop_daemon, is_running

        if not is_running():
            _print("\n  [dim]No daemon running.[/]\n")
            return

        pid = stop_daemon()
        if pid:
            _print(f"\n  [green]Daemon stopped[/] (was PID {pid})\n")
        else:
            _print("\n  [dim]Daemon was not running.[/]\n")

    except Exception as exc:
        _print(f"\n  [red]Error:[/] {exc}\n")
        sys.exit(1)


@daemon.command("status")
def daemon_status_cmd() -> None:
    """Show the daemon status.

    Checks the PID file and reports whether the daemon is running.

    Examples:

        skchat daemon status
    """
    from .daemon import daemon_status

    info = daemon_status()
    _print("")

    if HAS_RICH and console:
        from rich.panel import Panel as _Panel

        running_str = "[green]running[/]" if info["running"] else "[red]stopped[/]"
        pid_str = str(info["pid"]) if info["pid"] else "[dim]none[/]"

        console.print(_Panel(
            f"[bold]Status:[/]   {running_str}\n"
            f"[bold]PID:[/]      {pid_str}\n"
            f"[bold]PID file:[/] [dim]{info['pid_file']}[/]\n"
            f"[bold]Log file:[/] [dim]{info['log_file']}[/]",
            title="SKChat Daemon",
            border_style="bright_blue",
        ))
    else:
        status_str = "running" if info["running"] else "stopped"
        _print(f"  Status:   {status_str}")
        _print(f"  PID:      {info['pid'] or 'none'}")
        _print(f"  PID file: {info['pid_file']}")
        _print(f"  Log file: {info['log_file']}")

    _print("")


@main.group()
def group() -> None:
    """Manage group chats.

    Create groups, add/remove members, and send group messages.
    Groups use AES-256-GCM encryption with PGP key distribution.

    Examples:

        skchat group create "Project Alpha"

        skchat group add-member GROUP_ID capauth:bob@skworld.io

        skchat group send GROUP_ID "Hello team!"

        skchat group list

        skchat group info GROUP_ID
    """


def _store_group(grp: "GroupChat") -> str:
    """Persist a GroupChat in thread metadata and as a JSON file.

    The GroupChat is serialized into the Thread's metadata dict under
    the ``group_data`` key so it can be fully reconstructed later via
    ``_load_group``.  A JSON copy is also written to
    ``~/.skchat/groups/<group_id>.json`` so the MCP server can discover
    groups created from the CLI.

    Args:
        grp: The GroupChat to persist.

    Returns:
        str: The memory ID assigned to this group record.
    """
    history = _get_history()
    thread = grp.to_thread()
    thread.metadata["group_data"] = grp.model_dump(mode="json")
    mem_id = history.store_thread(thread)

    # Mirror to groups dir so MCP server (which reads *.json files) can find it.
    try:
        groups_dir = Path(SKCHAT_HOME).expanduser() / "groups"
        groups_dir.mkdir(parents=True, exist_ok=True)
        (groups_dir / f"{grp.id}.json").write_text(
            grp.model_dump_json(indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # Non-fatal — primary store succeeded

    return mem_id


def _load_group(group_id: str) -> "Optional[GroupChat]":
    """Load a GroupChat from storage by its ID.

    Searches thread metadata for a serialized GroupChat matching
    the given ID. Returns None if not found.

    Args:
        group_id: The group UUID (or prefix).

    Returns:
        Optional[GroupChat]: The reconstructed group, or None.
    """
    from .group import GroupChat

    history = _get_history()
    thread_data = history.get_thread_meta(group_id)
    if thread_data is not None:
        group_data = thread_data.get("group_data")
        if group_data is not None:
            return GroupChat.model_validate(group_data)

    # Fallback: load from ~/.skchat/groups/<group_id>.json
    groups_dir = Path(SKCHAT_HOME).expanduser() / "groups"
    json_path = groups_dir / f"{group_id}.json"
    if json_path.exists():
        try:
            return GroupChat.model_validate_json(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return None


@group.command("create")
@click.argument("name")
@click.option("--description", "-d", default="", help="Group description.")
def group_create(name: str, description: str) -> None:
    """Create a new group chat.

    You become the admin of the new group. Add members with
    'skchat group add-member'.

    Examples:

        skchat group create "Project Alpha"

        skchat group create "Sovereign Squad" -d "Core team chat"
    """
    from .group import GroupChat

    identity = _get_identity()
    grp = GroupChat.create(
        name=name,
        creator_uri=identity,
        description=description,
    )

    _store_group(grp)

    _print("")
    if HAS_RICH and console:
        console.print(Panel(
            f"[bold]Name:[/] [cyan]{grp.name}[/]\n"
            f"[bold]ID:[/] [dim]{grp.id}[/]\n"
            f"[bold]Admin:[/] {identity}\n"
            f"[bold]Description:[/] {description or '[dim]none[/]'}\n"
            f"[bold]Key version:[/] {grp.key_version}",
            title="Group Created",
            border_style="green",
        ))
    else:
        _print(f"  Created group '{name}' (ID: {grp.id[:12]})")
        _print(f"  Admin: {identity}")
    _print("")


@group.command("list")
@click.option("--limit", "-n", default=20, help="Max groups to show.")
def group_list(limit: int) -> None:
    """List all known group chats.

    Examples:

        skchat group list
    """
    history = _get_history()
    threads = history.list_threads(limit=limit)

    group_threads = [
        t for t in threads
        if t.get("participants") and len(t.get("participants", [])) > 0
    ]

    _print("")
    if not group_threads:
        _print("  [dim]No groups found.[/]")
        _print("")
        return

    if HAS_RICH and console:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"Groups ({len(group_threads)})",
        )
        table.add_column("ID", style="dim", max_width=12)
        table.add_column("Name", style="bold", max_width=30)
        table.add_column("Members", justify="right")
        table.add_column("Messages", justify="right")

        for t in group_threads:
            tid = (t.get("thread_id") or "")[:12]
            title = t.get("title", "Untitled")
            members = str(len(t.get("participants", [])))
            count = str(t.get("message_count", 0))
            table.add_row(tid, title, members, count)

        console.print(table)
    else:
        for t in group_threads:
            title = t.get("title", "Untitled")
            count = t.get("message_count", 0)
            members = len(t.get("participants", []))
            _print(f"  {title} ({members} members, {count} messages)")

    _print("")


@group.command("send")
@click.argument("group_id")
@click.argument("message")
@click.option("--ttl", type=int, default=None, help="Seconds until auto-delete.")
def group_send(group_id: str, message: str, ttl: Optional[int]) -> None:
    """Send a message to a group, broadcasting to ALL members.

    Extracts @mentions and stores them in message metadata so mentioned
    members can receive targeted notifications.

    Examples:

        skchat group send abc123 "Hello team!"

        skchat group send abc123 "@lumina @claude Review this!" --ttl 60
    """
    from .models import ChatMessage, ContentType

    identity = _get_identity()
    history = _get_history()

    # Extract @mentions (e.g. @lumina, @claude, @opus)
    mentions = re.findall(r"@(\w+)", message)

    grp = _load_group(group_id)

    if grp is not None:
        # Compose via group (validates membership, increments message_count)
        msg = grp.compose_group_message(sender_uri=identity, content=message, ttl=ttl)
        if msg is None:
            # Sender is not a member — compose a bare message and store locally
            msg = ChatMessage(
                sender=identity,
                recipient=f"group:{group_id}",
                content=message,
                content_type=ContentType.MARKDOWN,
                thread_id=group_id,
                ttl=ttl,
                metadata={"group_message": True},
            )
        if mentions:
            msg.metadata["mentions"] = mentions

        history.store_message(msg)

        # Broadcast individually to every member (skip self)
        delivered_names: list[str] = []
        failed_names: list[str] = []
        for member in grp.members:
            if member.identity_uri == identity:
                continue
            member_msg = msg.model_copy(update={"recipient": member.identity_uri})
            result = _try_deliver(member_msg)
            label = member.display_name or member.identity_uri.split(":")[-1]
            if result.get("delivered"):
                delivered_names.append(label)
            else:
                failed_names.append(label)

        _store_group(grp)

        _print("")
        total = len(grp.members) - 1  # exclude self
        mention_note = (
            f" · mentions: {', '.join('@' + m for m in mentions)}" if mentions else ""
        )
        if delivered_names:
            _print(
                f"  [green]Broadcast to {len(delivered_names)}/{total} members[/]"
                f"{mention_note}"
            )
        else:
            _print(
                f"  [yellow]Stored locally[/] — 0/{total} delivered{mention_note}"
            )
        if failed_names:
            _print(f"  [dim]No transport for: {', '.join(failed_names)}[/]")
        _print("")

    else:
        # Group not found locally — fall back to a single group-addressed message
        meta: dict = {"group_message": True}
        if mentions:
            meta["mentions"] = mentions
        msg = ChatMessage(
            sender=identity,
            recipient=f"group:{group_id}",
            content=message,
            content_type=ContentType.MARKDOWN,
            thread_id=group_id,
            ttl=ttl,
            metadata=meta,
        )
        history.store_message(msg)
        transport_info = _try_deliver(msg)

        _print("")
        if transport_info.get("delivered"):
            _print(
                f"  [green]Sent to group {group_id[:12]}[/]"
                f" via {transport_info.get('transport')}"
            )
        else:
            _print(
                f"  [yellow]Stored locally[/] (group not found,"
                f" {transport_info.get('error', 'no transport')})"
            )
        _print("")


@group.command("add-member")
@click.argument("group_id")
@click.argument("identity")
@click.option(
    "--role",
    type=click.Choice(["admin", "member", "observer"], case_sensitive=False),
    default="member",
    help="Member role (default: member).",
)
@click.option(
    "--type",
    "ptype",
    type=click.Choice(["human", "agent", "service"], case_sensitive=False),
    default="human",
    help="Participant type (default: human).",
)
@click.option("--display-name", "-n", default="", help="Display name for the member.")
def group_add_member(
    group_id: str, identity: str, role: str, ptype: str, display_name: str,
) -> None:
    """Add a member to an existing group.

    The identity can be a full capauth URI or a friendly peer name.

    Examples:

        skchat group add-member abc123 capauth:bob@skworld.io

        skchat group add-member abc123 lumina --type agent

        skchat group add-member abc123 alice --role admin
    """
    from .group import MemberRole, ParticipantType

    try:
        resolved = resolve_peer_name(identity)
    except PeerResolutionError:
        resolved = identity

    grp = _load_group(group_id)
    if grp is None:
        _print(f"\n  [red]Error:[/] Group '{group_id[:12]}' not found.\n")
        sys.exit(1)

    role_map = {"admin": MemberRole.ADMIN, "member": MemberRole.MEMBER, "observer": MemberRole.OBSERVER}
    type_map = {"human": ParticipantType.HUMAN, "agent": ParticipantType.AGENT, "service": ParticipantType.SERVICE}

    member = grp.add_member(
        identity_uri=resolved,
        role=role_map[role],
        participant_type=type_map[ptype],
        display_name=display_name,
    )

    if member is None:
        _print(f"\n  [yellow]Already a member:[/] {resolved}\n")
        return

    _store_group(grp)

    _print("")
    if HAS_RICH and console:
        console.print(Panel(
            f"[bold]Group:[/] [cyan]{grp.name}[/]\n"
            f"[bold]Added:[/] {resolved}\n"
            f"[bold]Role:[/] {role}\n"
            f"[bold]Type:[/] {ptype}\n"
            f"[bold]Members:[/] {grp.member_count}",
            title="Member Added",
            border_style="green",
        ))
    else:
        _print(f"  Added {resolved} to '{grp.name}' as {role}")
    _print("")


@group.command("remove-member")
@click.argument("group_id")
@click.argument("identity")
def group_remove_member(group_id: str, identity: str) -> None:
    """Remove a member from a group and rotate the group key.

    Forward secrecy: the group key is automatically rotated so the
    removed member cannot decrypt future messages.

    Examples:

        skchat group remove-member abc123 capauth:bob@skworld.io

        skchat group remove-member abc123 bob
    """
    try:
        resolved = resolve_peer_name(identity)
    except PeerResolutionError:
        resolved = identity

    grp = _load_group(group_id)
    if grp is None:
        _print(f"\n  [red]Error:[/] Group '{group_id[:12]}' not found.\n")
        sys.exit(1)

    removed = grp.remove_member(resolved)
    if not removed:
        _print(f"\n  [yellow]Not a member:[/] {resolved}\n")
        return

    _store_group(grp)

    _print("")
    if HAS_RICH and console:
        console.print(Panel(
            f"[bold]Group:[/] [cyan]{grp.name}[/]\n"
            f"[bold]Removed:[/] {resolved}\n"
            f"[bold]Key rotated:[/] v{grp.key_version}\n"
            f"[bold]Remaining:[/] {grp.member_count}",
            title="Member Removed",
            border_style="yellow",
        ))
    else:
        _print(f"  Removed {resolved} from '{grp.name}' (key rotated to v{grp.key_version})")
    _print("")


@group.command("rotate-key")
@click.argument("group_id")
@click.option("--reason", "-r", default="manual", help="Reason for key rotation.")
def group_rotate_key(group_id: str, reason: str) -> None:
    """Manually rotate the group encryption key.

    All remaining members will be notified of the new key version.
    Use after a security concern or for periodic key hygiene.

    Examples:

        skchat group rotate-key abc123

        skchat group rotate-key abc123 --reason "security-concern"
    """
    grp = _load_group(group_id)
    if grp is None:
        _print(f"\n  [red]Error:[/] Group '{group_id[:12]}' not found.\n")
        sys.exit(1)

    old_version = grp.key_version
    grp.rotate_key(reason=reason)
    _store_group(grp)

    _print("")
    if HAS_RICH and console:
        console.print(Panel(
            f"[bold]Group:[/] [cyan]{grp.name}[/]\n"
            f"[bold]Key version:[/] v{old_version} → v{grp.key_version}\n"
            f"[bold]Reason:[/] {reason}\n"
            f"[bold]Members:[/] {grp.member_count}",
            title="Key Rotated",
            border_style="green",
        ))
    else:
        _print(f"  Key rotated for '{grp.name}' (v{old_version} → v{grp.key_version}, reason: {reason})")
    _print("")


@group.command("info")
@click.argument("group_id")
def group_info(group_id: str) -> None:
    """Show detailed information about a group.

    Examples:

        skchat group info abc123
    """
    grp = _load_group(group_id)
    if grp is None:
        _print(f"\n  [red]Error:[/] Group '{group_id[:12]}' not found.\n")
        sys.exit(1)

    _print("")
    if HAS_RICH and console:
        members_list = "\n".join(
            f"  {m.display_name} [{m.role.value}] ({m.participant_type.value})"
            for m in grp.members
        )
        console.print(Panel(
            f"[bold]Name:[/] [cyan]{grp.name}[/]\n"
            f"[bold]ID:[/] [dim]{grp.id}[/]\n"
            f"[bold]Description:[/] {grp.description or '[dim]none[/]'}\n"
            f"[bold]Created by:[/] {grp.created_by}\n"
            f"[bold]Created:[/] {grp.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"[bold]Updated:[/] {grp.updated_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"[bold]Messages:[/] {grp.message_count}\n"
            f"[bold]Key version:[/] {grp.key_version}\n"
            f"[bold]Members ({grp.member_count}):[/]\n{members_list}",
            title="Group Info",
            border_style="cyan",
        ))
    else:
        _print(grp.summary())
    _print("")


@group.command("members")
@click.argument("group_id")
def group_members(group_id: str) -> None:
    """List members of a group with roles and types.

    Examples:

        skchat group members abc123
    """
    grp = _load_group(group_id)
    if grp is None:
        _print(f"\n  [red]Error:[/] Group '{group_id[:12]}' not found.\n")
        sys.exit(1)

    _print("")
    if HAS_RICH and console:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"Members of {grp.name} ({grp.member_count})",
        )
        table.add_column("Name", style="bold", max_width=25)
        table.add_column("Identity", style="cyan", max_width=35)
        table.add_column("Role", max_width=10)
        table.add_column("Type", max_width=10)
        table.add_column("Joined", style="dim", max_width=12)
        table.add_column("Scope", style="dim", max_width=30)

        for m in grp.members:
            role_style = "green" if m.role.value == "admin" else ""
            scope_str = ", ".join(m.tool_scope) if m.tool_scope else "unrestricted"
            joined_str = m.joined_at.strftime("%Y-%m-%d")
            table.add_row(
                m.display_name,
                m.identity_uri,
                Text(m.role.value, style=role_style),
                m.participant_type.value,
                joined_str,
                scope_str,
            )

        console.print(table)
    else:
        for m in grp.members:
            scope_str = ", ".join(m.tool_scope) if m.tool_scope else "unrestricted"
            joined_str = m.joined_at.strftime("%Y-%m-%d")
            _print(f"  {m.display_name} ({m.role.value}, {m.participant_type.value}) joined={joined_str} scope={scope_str}")
    _print("")


@group.command("set-role")
@click.argument("group_id")
@click.argument("identity")
@click.argument("role", type=click.Choice(["admin", "member", "observer"], case_sensitive=False))
def group_set_role(group_id: str, identity: str, role: str) -> None:
    """Change a member's role in a group.

    Only admins can change roles. Roles: admin, member, observer.

    Examples:

        skchat group set-role abc123 capauth:bob@skworld.io admin

        skchat group set-role abc123 alice observer
    """
    from .group import MemberRole

    try:
        resolved = resolve_peer_name(identity)
    except PeerResolutionError:
        resolved = identity

    grp = _load_group(group_id)
    if grp is None:
        _print(f"\n  [red]Error:[/] Group '{group_id[:12]}' not found.\n")
        sys.exit(1)

    caller = _get_identity()
    if not grp.is_admin(caller):
        _print(f"\n  [red]Error:[/] You are not an admin of this group.\n")
        sys.exit(1)

    member = grp.get_member(resolved)
    if member is None:
        _print(f"\n  [red]Error:[/] '{resolved}' is not a member of this group.\n")
        sys.exit(1)

    role_map = {"admin": MemberRole.ADMIN, "member": MemberRole.MEMBER, "observer": MemberRole.OBSERVER}
    member.role = role_map[role]
    grp.updated_at = datetime.now(timezone.utc)
    _store_group(grp)

    _print("")
    if HAS_RICH and console:
        console.print(Panel(
            f"[bold]Group:[/] [cyan]{grp.name}[/]\n"
            f"[bold]Member:[/] {resolved}\n"
            f"[bold]New role:[/] {role}",
            title="Role Updated",
            border_style="green",
        ))
    else:
        _print(f"  Set {resolved} role to {role} in '{grp.name}'")
    _print("")


@group.command("set-tool-scope")
@click.argument("group_id")
@click.argument("identity")
@click.argument("tools", nargs=-1)
@click.option("--clear", is_flag=True, help="Clear scope (unrestricted access).")
def group_set_tool_scope(
    group_id: str, identity: str, tools: tuple[str, ...], clear: bool,
) -> None:
    """Set the tool scope for a member in a group.

    Tool scope controls which skills/tools a member can invoke.
    An empty scope means unrestricted access. Only admins can set scope.
    Scope gates actions, never speech.

    Examples:

        skchat group set-tool-scope abc123 lumina skseal.sign skmemory.search

        skchat group set-tool-scope abc123 jarvis --clear
    """
    try:
        resolved = resolve_peer_name(identity)
    except PeerResolutionError:
        resolved = identity

    grp = _load_group(group_id)
    if grp is None:
        _print(f"\n  [red]Error:[/] Group '{group_id[:12]}' not found.\n")
        sys.exit(1)

    caller = _get_identity()
    scope = [] if clear else list(tools)

    ok = grp.set_tool_scope(resolved, scope, by_admin=caller)
    if not ok:
        if not grp.is_admin(caller):
            _print(f"\n  [red]Error:[/] You are not an admin of this group.\n")
        else:
            _print(f"\n  [red]Error:[/] '{resolved}' is not a member of this group.\n")
        sys.exit(1)

    _store_group(grp)

    scope_display = ", ".join(scope) if scope else "unrestricted"
    _print("")
    if HAS_RICH and console:
        console.print(Panel(
            f"[bold]Group:[/] [cyan]{grp.name}[/]\n"
            f"[bold]Member:[/] {resolved}\n"
            f"[bold]Tool scope:[/] {scope_display}",
            title="Tool Scope Updated",
            border_style="green",
        ))
    else:
        _print(f"  Set tool scope for {resolved}: {scope_display}")
    _print("")


@group.command("quick-start")
@click.argument("name")
@click.argument("members", nargs=-1, required=True)
@click.option("--description", "-d", default="", help="Group description.")
def group_quick_start(name: str, members: tuple[str, ...], description: str) -> None:
    """Create a group and add members in one step.

    Resolves peer names automatically. The creator is added as admin;
    all other members join as regular members.

    Examples:

        skchat group quick-start "Sovereign Squad" lumina opus

        skchat group quick-start "Dev Team" jarvis lumina --type agent

        skchat group quick-start "Three-way" lumina user@skworld.io -d "Main chat"
    """
    from .group import GroupChat, ParticipantType

    identity = _get_identity()
    grp = GroupChat.create(
        name=name,
        creator_uri=identity,
        description=description,
    )

    resolved_members = []
    for member_name in members:
        try:
            resolved = resolve_peer_name(member_name)
        except PeerResolutionError:
            resolved = member_name

        if resolved == identity:
            continue

        # Heuristic: names containing common agent identifiers get AGENT type
        agent_hints = {"jarvis", "lumina", "opus", "sonnet", "ava", "cursor-agent"}
        base_name = resolved.split(":")[-1].split("@")[0].lower()
        ptype = ParticipantType.AGENT if base_name in agent_hints else ParticipantType.HUMAN

        member = grp.add_member(
            identity_uri=resolved,
            participant_type=ptype,
            display_name=member_name if member_name != resolved else "",
        )
        if member:
            resolved_members.append((member_name, resolved, ptype.value))

    _store_group(grp)

    _print("")
    if HAS_RICH and console:
        members_list = "\n".join(
            f"  + {name} ({uri}) [{ptype}]"
            for name, uri, ptype in resolved_members
        )
        console.print(Panel(
            f"[bold]Name:[/] [cyan]{grp.name}[/]\n"
            f"[bold]ID:[/] [dim]{grp.id}[/]\n"
            f"[bold]Admin:[/] {identity}\n"
            f"[bold]Members ({grp.member_count}):[/]\n{members_list}\n"
            f"[bold]Description:[/] {description or '[dim]none[/]'}\n\n"
            f"[dim]Send a message: skchat group send {grp.id[:12]} \"Hello team!\"[/]",
            title="Group Ready",
            border_style="green",
        ))
    else:
        _print(f"  Created group '{name}' with {grp.member_count} members (ID: {grp.id[:12]})")
        for name, uri, ptype in resolved_members:
            _print(f"    + {name} ({ptype})")
    _print("")


@main.command()
@click.option(
    "--url",
    default="http://127.0.0.1:9384",
    show_default=True,
    help="SKComm base URL.",
)
def health(url: str) -> None:
    """Show transport health: WebRTC, file fallback, and daemon uptime.

    Checks SKComm HTTP health, WebRTC signaling endpoint, and ICE config.
    File transport is always reported as available.
    """
    from .watchdog import TransportWatchdog

    wd = TransportWatchdog(transport=None, skcomm_url=url)
    summary = wd.health_summary()

    webrtc = summary["webrtc"]
    uptime = summary["uptime_seconds"]
    peers = webrtc["active_peers"]

    _print("")
    if HAS_RICH and console:

        def _ok(v: bool) -> str:
            return "[green]OK[/]" if v else "[red]DOWN[/]"

        console.print(
            f"[bold]Transport status:[/]  {summary['transport_status']}\n"
            f"\n"
            f"[bold]SKComm:[/]            {_ok(summary['skcomm_ok'])}\n"
            f"[bold]WebRTC:[/]            signaling {_ok(webrtc['signaling_ok'])}"
            f"  |  ICE servers {_ok(webrtc['ice_servers_configured'])}"
            f"  |  active peers [cyan]{peers}[/]\n"
            f"[bold]File transport:[/]    [green]always available[/]\n"
            f"\n"
            f"[bold]Daemon uptime:[/]     {uptime:.1f}s  |  "
            f"[bold]Consecutive failures:[/] {summary['consecutive_failures']}",
        )
    else:

        def _ok_plain(v: bool) -> str:
            return "OK" if v else "DOWN"

        _print(f"  Transport status:  {summary['transport_status']}")
        _print(f"  SKComm:            {_ok_plain(summary['skcomm_ok'])}")
        _print(
            f"  WebRTC:            signaling={_ok_plain(webrtc['signaling_ok'])}"
            f"  ice={_ok_plain(webrtc['ice_servers_configured'])}"
            f"  peers={peers}"
        )
        _print("  File transport:    always available")
        _print(f"  Daemon uptime:     {uptime:.1f}s")
        _print(f"  Failures:          {summary['consecutive_failures']}")
    _print("")


@main.command()
def status() -> None:
    """Show SKChat status and statistics.

    Displays a rich dashboard with daemon health, identity, message
    counts, advocacy stats, online peers, recent activity, and
    transport availability.
    """
    import unicodedata

    identity = _get_identity()
    chat_history = _get_history()

    from .daemon import daemon_status
    ds = daemon_status()

    # ── Box layout constants ──────────────────────────────────────
    TOTAL_W = 47   # full line width including ╔/╗
    INNER_W = TOTAL_W - 2   # 45 — space between ║ and ║
    TEXT_W  = INNER_W - 2   # 43 — text after leading space, before trailing space

    def _wlen(s: str) -> int:
        """Visual column width, accounting for wide Unicode (emoji etc.)."""
        w = 0
        for ch in s:
            eaw = unicodedata.east_asian_width(ch)
            w += 2 if eaw in ("W", "F") else 1
        return w

    def _row(plain: str, styled: str = "") -> str:
        """Render one content row: ║ <styled> <padding> ║"""
        if not styled:
            styled = plain
        pad = TEXT_W - _wlen(plain)
        return "║ " + styled + " " * max(0, pad) + " ║"

    def _section(title: str, *, top: bool = False) -> str:
        """╠══ Title ══╣  (or ╔/╗ for top)."""
        fill = INNER_W - len(title) - 2   # 2 for surrounding spaces
        left = fill // 2
        right = fill - left
        lc, rc = ("╔", "╗") if top else ("╠", "╣")
        return lc + "═" * left + " " + title + " " + "═" * right + rc

    _bot = "╚" + "═" * INNER_W + "╝"

    # ── Data gathering ────────────────────────────────────────────
    running        = ds.get("running", False)
    uptime_s       = int(ds.get("uptime_seconds", 0))
    msg_recv       = int(ds.get("messages_received", 0))
    msg_sent       = int(ds.get("messages_sent", 0))
    advocacy_count = int(ds.get("advocacy_responses", 0))
    webrtc_ok      = bool(ds.get("webrtc_signaling_ok", False))

    def _fmt_uptime(s: int) -> str:
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        return f"{s // 3600}h {(s % 3600) // 60}m"

    def _fmt_age(s: float) -> str:
        if s < 60:
            return f"{int(s)}s ago"
        if s < 3600:
            return f"{int(s / 60)}m ago"
        return f"{int(s / 3600)}h ago"

    # Lumina-bridge PID
    lumina_pid: Optional[int] = None
    try:
        ps_lines = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        ).stdout.splitlines()
        for line in ps_lines:
            if "lumina-bridge.py" in line and "grep" not in line:
                parts = line.split()
                if len(parts) > 1:
                    try:
                        lumina_pid = int(parts[1])
                    except ValueError:
                        lumina_pid = -1
                break
    except Exception:
        pass

    # Syncthing
    syncthing_ok = False
    try:
        syncthing_ok = (
            subprocess.run(
                ["pgrep", "-x", "syncthing"],
                capture_output=True, timeout=3,
            ).returncode == 0
        )
    except Exception:
        pass

    # Presence peers
    peers_info: list[tuple[str, str, str]] = []  # (uri, status, age_str)
    try:
        from .presence import PresenceCache
        pc = PresenceCache()
        all_peers = pc.get_all()
        now = datetime.now(timezone.utc)
        for uri, entry in all_peers.items():
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                age_s = (now - ts).total_seconds()
                age_str = _fmt_age(age_s)
                st = pc.get_status(uri)
            except Exception:
                age_str = ""
                st = "offline"
            peers_info.append((uri, st, age_str))
        peers_info.sort(
            key=lambda x: (0 if x[1] == "online" else 1 if x[1] == "away" else 2)
        )
    except Exception:
        pass

    # Recent messages
    recent_msgs: list[dict] = []
    try:
        recent_msgs = chat_history.get_messages_since(minutes=120, limit=100)[-4:]
    except Exception:
        pass

    # ── Build output lines ────────────────────────────────────────
    out: list[str] = []

    out.append(_section("SKChat Status", top=True))

    # Daemon
    if running:
        up_str = _fmt_uptime(uptime_s)
        d_plain  = f"Daemon:     \u2713 RUNNING (uptime: {up_str})"
        d_styled = (
            "Daemon:     "
            + click.style("\u2713 RUNNING", fg="green", bold=True)
            + f" (uptime: {up_str})"
        )
    else:
        d_plain  = "Daemon:     \u2717 STOPPED"
        d_styled = "Daemon:     " + click.style("\u2717 STOPPED", fg="red", bold=True)
    out.append(_row(d_plain, d_styled))

    # Identity
    out.append(_row(
        f"Identity:   {identity}",
        "Identity:   " + click.style(identity, fg="cyan"),
    ))

    # Messages (fall back to history count when daemon not running)
    if not running and msg_recv == 0:
        try:
            msg_recv = chat_history.message_count()
        except Exception:
            pass
    out.append(_row(
        f"Messages:   {msg_recv} received, {msg_sent} sent",
        "Messages:   "
        + click.style(str(msg_recv), fg="yellow") + " received, "
        + click.style(str(msg_sent), fg="yellow") + " sent",
    ))

    # Advocacy
    if advocacy_count:
        adv_plain  = f"Advocacy:   \u2713 active ({advocacy_count} auto-replies)"
        adv_styled = (
            "Advocacy:   "
            + click.style("\u2713 active", fg="green")
            + f" ({advocacy_count} auto-replies)"
        )
    else:
        adv_plain  = "Advocacy:   active (no auto-replies yet)"
        adv_styled = (
            "Advocacy:   "
            + click.style("active", fg="green")
            + " (no auto-replies yet)"
        )
    out.append(_row(adv_plain, adv_styled))

    # Lumina bridge
    if lumina_pid:
        br_plain  = f"Bridge:     \u2713 lumina-bridge (pid: {lumina_pid})"
        br_styled = (
            "Bridge:     "
            + click.style("\u2713 lumina-bridge", fg="green")
            + f" (pid: {lumina_pid})"
        )
    else:
        br_plain  = "Bridge:     \u2717 lumina-bridge not running"
        br_styled = "Bridge:     " + click.style("\u2717 lumina-bridge not running", fg="red")
    out.append(_row(br_plain, br_styled))

    # ── Online Now ────────────────────────────────────────────────
    out.append(_section("Online Now"))
    if peers_info:
        for uri, pst, age_str in peers_info[:5]:
            name = uri.split("@")[0].replace("capauth:", "").replace("nostr:", "")
            name = name[:14]
            if pst == "online":
                icon, col, label = "\u25cf", "green", (f"online ({age_str})" if age_str else "online")
            elif pst == "away":
                icon, col, label = "\u25cf", "yellow", (f"away ({age_str})" if age_str else "away")
            else:
                icon, col, label = "\u25cb", "white", "offline"
            peer_plain  = f"  {icon} {name:<14} {label}"
            peer_styled = (
                "  "
                + click.style(icon, fg=col)
                + f" {name:<14} "
                + click.style(label, fg=col)
            )
            out.append(_row(peer_plain, peer_styled))
    else:
        out.append(_row(
            "  (no peers in presence cache)",
            "  " + click.style("(no peers in presence cache)", dim=True),
        ))

    # ── Recent Activity ───────────────────────────────────────────
    out.append(_section("Recent Activity"))
    my_short = identity.split("@")[0].replace("capauth:", "")
    if recent_msgs:
        for msg in recent_msgs:
            try:
                ts = msg.get("timestamp")
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                if ts and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                time_str = ts.strftime("%H:%M") if ts else "??:??"
            except Exception:
                time_str = "??:??"

            sender  = msg.get("sender") or "?"
            s_short_full = sender.split("@")[0].replace("capauth:", "").replace("nostr:", "")
            s_short = s_short_full[:16]
            content = str(msg.get("content") or "")

            if s_short_full == my_short:
                recip   = (msg.get("recipient") or "?")
                r_short = recip.split("@")[0].replace("capauth:", "").replace("nostr:", "").replace("group:", "grp/")[:16]
                label_plain  = f"You \u2192 {r_short}"
                label_styled = (
                    click.style("You", fg="blue")
                    + " \u2192 "
                    + click.style(r_short, fg="cyan")
                )
            else:
                label_plain  = s_short
                label_styled = click.style(s_short, fg="magenta")

            max_body = TEXT_W - len(time_str) - len(label_plain) - 5
            if len(content) > max(max_body, 6):
                content = content[: max(max_body - 2, 6)] + ".."

            act_plain  = f"[{time_str}] {label_plain}: {content}"
            act_styled = (
                click.style(f"[{time_str}]", dim=True)
                + f" {label_styled}: "
                + click.style(content, dim=True)
            )
            out.append(_row(act_plain, act_styled))
    else:
        out.append(_row(
            "  (no recent messages)",
            "  " + click.style("(no recent messages)", dim=True),
        ))

    # ── Transports ────────────────────────────────────────────────
    out.append(_section("Transports"))

    def _t(name: str, ok: bool) -> tuple[str, str]:
        mark = "\u2713" if ok else "\u2717"
        col  = "green" if ok else "red"
        return f"{name} {mark}", f"{name} " + click.style(mark, fg=col)

    t_items = [
        _t("syncthing", syncthing_ok),
        _t("file", True),
        _t("webrtc", webrtc_ok),
    ]
    tr_plain  = "  " + "  \u2502  ".join(p for p, _ in t_items)
    tr_styled = "  " + "  \u2502  ".join(s for _, s in t_items)
    out.append(_row(tr_plain, tr_styled))

    out.append(_bot)

    _print("")
    for line in out:
        click.echo(line)
    _print("")


@main.command()
@click.argument("group_id")
@click.option(
    "--duration",
    "-d",
    type=int,
    default=None,
    metavar="SECONDS",
    help="Fixed recording duration in seconds (default: interactive, press Enter to stop).",
)
@click.option(
    "--whisper-model",
    "-m",
    "whisper_model",
    default="base",
    show_default=True,
    help="Whisper model for transcription (tiny/base/small/medium/large).",
)
def voice(group_id: str, duration: Optional[int], whisper_model: str) -> None:
    """Record a voice message and send it to a group.

    Records audio via microphone, transcribes with Whisper STT, confirms
    the transcription, then sends the text to the given group.

    Requires arecord (alsa-utils) and openai-whisper
    (pip install openai-whisper).

    Examples:

        skchat voice abc123

        skchat voice abc123 --duration 30

        skchat voice abc123 --whisper-model small
    """
    from .voice import VoiceRecorder

    recorder = VoiceRecorder(whisper_model=whisper_model)
    if not recorder.available:
        _print("\n  [red]Error:[/] openai-whisper is not installed.")
        _print("  Install with: [cyan]pip install openai-whisper[/]\n")
        sys.exit(1)

    if duration is not None:
        _print(f"\n  [cyan]Recording for {duration}s...[/]\n")
        text = recorder.record(duration=duration)
    else:
        _print("\n  [cyan]Recording...[/] press Enter to stop\n")
        text = recorder.record_interactive()

    if not text:
        _print("\n  [red]No transcription.[/] Recording failed or captured silence.\n")
        sys.exit(1)

    _print(f"\n  [bold]Transcription:[/] {text}")
    if not click.confirm("  Send to group?", default=True):
        _print("  [dim]Cancelled.[/]\n")
        return

    msg = ChatMessage(
        sender=_get_identity(),
        recipient=f"group:{group_id}",
        content=text,
        content_type=ContentType.PLAIN,
        thread_id=group_id,
        metadata={"voice_transcribed": True, "whisper_model": whisper_model},
    )

    history = _get_history()
    mem_id = history.store_message(msg)
    transport_info = _try_deliver(msg)

    _print("")
    if transport_info.get("delivered"):
        _print(f"  [green]Sent to group {group_id[:12]}[/] via {transport_info.get('transport')}")
    else:
        _print(f"  [yellow]Stored locally[/] ({transport_info.get('error', 'no transport')})")
    _print(f"  [dim]Memory ID: {mem_id}[/]\n")


@main.group()
def config() -> None:
    """Show and validate SKChat configuration.

    Examples:

        skchat config show

        skchat config validate
    """


@config.command("show")
def config_show() -> None:
    """Display current configuration with resolved paths."""
    config_path = Path(SKCHAT_HOME).expanduser() / "config.yml"
    peers_dir = Path("~/.skcapstone/peers").expanduser()
    pid_file = Path(SKCHAT_HOME).expanduser() / "daemon.pid"
    memory_dir = Path(SKCHAT_HOME).expanduser() / "memory"
    identity_file = Path("~/.skcapstone/identity/identity.json").expanduser()

    _print("")
    if HAS_RICH and console:
        def _status(p: Path) -> str:
            return "[green]exists[/]" if p.exists() else "[red]missing[/]"

        raw_cfg = ""
        if config_path.exists():
            try:
                with open(config_path) as fh:
                    raw_cfg = "\n\n" + fh.read()[:800]
            except Exception:
                raw_cfg = "\n\n[red](could not read)[/]"

        console.print(Panel(
            f"[bold]Config file:[/]   {config_path}  {_status(config_path)}\n"
            f"[bold]Identity file:[/] {identity_file}  {_status(identity_file)}\n"
            f"[bold]Memory dir:[/]    {memory_dir}  {_status(memory_dir)}\n"
            f"[bold]PID file:[/]      {pid_file}  {_status(pid_file)}\n"
            f"[bold]Peers dir:[/]     {peers_dir}  {_status(peers_dir)}"
            + raw_cfg,
            title="SKChat Configuration",
            border_style="bright_blue",
        ))
    else:
        def _s(p: Path) -> str:
            return "exists" if p.exists() else "missing"

        _print(f"  Config:   {config_path}  [{_s(config_path)}]")
        _print(f"  Identity: {identity_file}  [{_s(identity_file)}]")
        _print(f"  Memory:   {memory_dir}  [{_s(memory_dir)}]")
        _print(f"  PID file: {pid_file}  [{_s(pid_file)}]")
        _print(f"  Peers:    {peers_dir}  [{_s(peers_dir)}]")
        if config_path.exists():
            try:
                with open(config_path) as fh:
                    _print("\n" + fh.read()[:800])
            except Exception:
                pass
    _print("")


@config.command("validate")
def config_validate() -> None:
    """Check all required files and directories exist.

    Exits with code 1 if any required item is missing.
    """
    config_path = Path(SKCHAT_HOME).expanduser() / "config.yml"
    peers_dir = Path("~/.skcapstone/peers").expanduser()
    identity_file = Path("~/.skcapstone/identity/identity.json").expanduser()
    memory_dir = Path(SKCHAT_HOME).expanduser() / "memory"

    checks = [
        ("Config file", config_path, config_path.is_file()),
        ("Identity file", identity_file, identity_file.is_file()),
        ("Memory dir", memory_dir, memory_dir.is_dir()),
        ("Peers dir", peers_dir, peers_dir.is_dir()),
        ("lumina peer", peers_dir / "lumina.json", (peers_dir / "lumina.json").is_file()),
        ("claude peer", peers_dir / "claude.json", (peers_dir / "claude.json").is_file()),
    ]

    _print("")
    all_ok = True
    for label, path, ok in checks:
        if HAS_RICH and console:
            marker = "[green]✓[/]" if ok else "[red]✗[/]"
            console.print(f"  {marker}  [bold]{label}:[/] [dim]{path}[/]")
        else:
            marker = "OK" if ok else "MISSING"
            _print(f"  [{marker}]  {label}: {path}")
        if not ok:
            all_ok = False

    _print("")
    if all_ok:
        if HAS_RICH and console:
            console.print("  [green]All checks passed.[/]\n")
        else:
            _print("  All checks passed.")
    else:
        if HAS_RICH and console:
            console.print("  [red]Some checks failed — review the items above.[/]\n")
        else:
            _print("  Some checks failed — review the items above.")
        sys.exit(1)


@main.command()
def who() -> None:
    """Show who is currently online.

    Reads the local presence cache and displays a table of known peers
    with their last-seen time and status.

    Colors: green = online (<2 min), yellow = away (<10 min), red = offline.

    Examples:

        skchat who
    """
    import json as _json
    from .presence import PresenceCache, PresenceState

    KNOWN_PEERS: dict[str, str] = {
        "capauth:lumina@skworld.io": "lumina",
        "capauth:claude@skworld.io": "claude",
        "chef@skworld.io": "chef",
    }

    peers_dir = Path.home() / ".skcapstone" / "peers"
    if peers_dir.exists():
        for peer_file in sorted(peers_dir.glob("*.json")):
            try:
                data = _json.loads(peer_file.read_text())
                uri = data.get("identity_uri") or data.get("uri", "")
                name = data.get("display_name") or data.get("name", peer_file.stem)
                if uri:
                    KNOWN_PEERS[uri] = name
            except Exception:
                pass

    cache = PresenceCache()
    all_entries = cache.get_all()
    all_uris = sorted(set(all_entries.keys()) | set(KNOWN_PEERS.keys()))

    if not all_uris:
        click.echo("No presence data. Is the daemon running?  (skchat daemon start)")
        return

    now = datetime.now(timezone.utc)
    click.echo("")
    click.echo(f"  {'Agent':<22} {'Last Seen':<12} Status")
    click.echo(f"  {'-'*22} {'-'*12} {'-'*10}")

    for uri in all_uris:
        display = KNOWN_PEERS.get(uri, uri.split("@")[0].replace("capauth:", ""))
        entry = all_entries.get(uri)
        if entry:
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                age = (now - ts).total_seconds()
                state_val = entry.get("state", "")
                if state_val == PresenceState.OFFLINE.value:
                    status = "offline"
                elif age <= 120:
                    status = "online"
                elif age <= 600:
                    status = "away"
                else:
                    status = "offline"
                last_seen = ts.strftime("%H:%M:%S")
            except Exception:
                status = "offline"
                last_seen = "-"
        else:
            status = "offline"
            last_seen = "-"

        if status == "online":
            status_str = click.style(status, fg="green")
        elif status == "away":
            status_str = click.style(status, fg="yellow")
        else:
            status_str = click.style(status, fg="red")

        click.echo(f"  {display:<22} {last_seen:<12} {status_str}")

    click.echo("")


@main.command()
def presence() -> None:
    """Show presence status of all known peers.

    Reads the local presence cache and displays a rich table of known peers
    with their last-seen time and online/away/offline status.

    Colors: green = online (<2 min), yellow = away (<10 min), red = offline.

    Examples:

        skchat presence
    """
    import json as _json
    from .presence import PresenceCache, PresenceState

    KNOWN_PEERS: dict[str, str] = {
        "capauth:lumina@skworld.io": "lumina",
        "capauth:claude@skworld.io": "claude",
        "chef@skworld.io": "chef",
    }

    peers_dir = Path.home() / ".skcapstone" / "peers"
    if peers_dir.exists():
        for peer_file in sorted(peers_dir.glob("*.json")):
            try:
                data = _json.loads(peer_file.read_text())
                uri = data.get("identity_uri") or data.get("uri", "")
                name = data.get("display_name") or data.get("name", peer_file.stem)
                if uri:
                    KNOWN_PEERS[uri] = name
            except Exception:
                pass

    cache = PresenceCache()
    all_entries = cache.get_all()
    all_uris = sorted(set(all_entries.keys()) | set(KNOWN_PEERS.keys()))

    if not all_uris:
        click.echo("No presence data. Is the daemon running?  (skchat daemon start)")
        return

    now = datetime.now(timezone.utc)

    if HAS_RICH:
        table = Table(title="Peer Presence", show_header=True, header_style="bold cyan")
        table.add_column("Agent", style="bold", min_width=20)
        table.add_column("Last Seen", min_width=10)
        table.add_column("Status", min_width=10)

        for uri in all_uris:
            display = KNOWN_PEERS.get(uri, uri.split("@")[0].replace("capauth:", ""))
            entry = all_entries.get(uri)
            if entry:
                try:
                    ts = datetime.fromisoformat(entry["timestamp"])
                    age = (now - ts).total_seconds()
                    state_val = entry.get("state", "")
                    if state_val == PresenceState.OFFLINE.value:
                        status = "offline"
                    elif age <= 120:
                        status = "online"
                    elif age <= 600:
                        status = "away"
                    else:
                        status = "offline"
                    last_seen = ts.strftime("%H:%M:%S")
                except Exception:
                    status = "offline"
                    last_seen = "-"
            else:
                status = "offline"
                last_seen = "-"

            if status == "online":
                status_cell = Text("● online", style="green")
            elif status == "away":
                status_cell = Text("◐ away", style="yellow")
            else:
                status_cell = Text("○ offline", style="red")

            table.add_row(display, last_seen, status_cell)

        console.print()
        console.print(table)
        console.print()
    else:
        click.echo("")
        click.echo(f"  {'Agent':<22} {'Last Seen':<12} Status")
        click.echo(f"  {'-'*22} {'-'*12} {'-'*10}")

        for uri in all_uris:
            display = KNOWN_PEERS.get(uri, uri.split("@")[0].replace("capauth:", ""))
            entry = all_entries.get(uri)
            if entry:
                try:
                    ts = datetime.fromisoformat(entry["timestamp"])
                    age = (now - ts).total_seconds()
                    state_val = entry.get("state", "")
                    if state_val == PresenceState.OFFLINE.value:
                        status = "offline"
                    elif age <= 120:
                        status = "online"
                    elif age <= 600:
                        status = "away"
                    else:
                        status = "offline"
                    last_seen = ts.strftime("%H:%M:%S")
                except Exception:
                    status = "offline"
                    last_seen = "-"
            else:
                status = "offline"
                last_seen = "-"

            if status == "online":
                status_str = click.style(status, fg="green")
            elif status == "away":
                status_str = click.style(status, fg="yellow")
            else:
                status_str = click.style(status, fg="red")

            click.echo(f"  {display:<22} {last_seen:<12} {status_str}")

        click.echo("")


@main.group()
def peers() -> None:
    """Discover and inspect known peers from the skcapstone peer store.

    Reads peer records from ~/.skcapstone/peers/ and displays them
    with identity URIs, trust levels, and entity types.

    Examples:

        skchat peers list

        skchat peers list --type ai-agent
    """


@peers.command("list")
@click.option(
    "--type",
    "entity_type",
    default=None,
    help="Filter by entity_type (e.g. ai-agent, human).",
)
def peers_list(entity_type: Optional[str]) -> None:
    """List all discovered peers from the skcapstone peer store.

    Reads JSON files from ~/.skcapstone/peers/ and displays a table
    of names, identity URIs, trust levels, and entity types.

    Examples:

        skchat peers list

        skchat peers list --type ai-agent
    """
    from .peer_discovery import PeerDiscovery

    disc = PeerDiscovery()
    peer_list = disc.list_peers()

    if entity_type:
        peer_list = [p for p in peer_list if p.get("entity_type", "").lower() == entity_type.lower()]

    _print("")
    if not peer_list:
        _print("  [dim]No peers found in ~/.skcapstone/peers/[/]")
        _print("")
        return

    if HAS_RICH and console:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"Peers ({len(peer_list)})",
        )
        table.add_column("Name", style="bold", max_width=16)
        table.add_column("URI", style="cyan", max_width=40)
        table.add_column("Type", max_width=12)
        table.add_column("Trust", max_width=10)
        table.add_column("Capabilities", style="dim", max_width=35)

        for peer in peer_list:
            name = peer.get("name", "?")
            uri = disc.resolve_identity(peer.get("handle", peer.get("name", ""))) or ""
            etype = peer.get("entity_type", "")
            trust = peer.get("trust_level", "")
            caps = ", ".join(peer.get("capabilities", []))
            if len(caps) > 35:
                caps = caps[:32] + "..."

            trust_style = "green" if trust == "verified" else "yellow"
            table.add_row(name, uri, etype, Text(trust, style=trust_style), caps)

        console.print(table)
    else:
        _print(f"  {len(peer_list)} peer(s):")
        for peer in peer_list:
            name = peer.get("name", "?")
            uri = disc.resolve_identity(peer.get("handle", peer.get("name", ""))) or ""
            trust = peer.get("trust_level", "")
            etype = peer.get("entity_type", "")
            _print(f"    {name:<16} {uri:<42} {etype:<12} {trust}")

    _print("")



@main.command()
@click.argument("message_id")
@click.argument("emoji")
def react(message_id: str, emoji: str) -> None:
    """Add an emoji reaction to a message.

    Examples:

        skchat react msg-abc123 👍

        skchat react msg-abc123 ❤️
    """
    identity = _get_identity()
    _reaction_store.add(message_id, emoji, identity)
    summary = _reaction_store.get_summary(message_id)
    rx_str = "  ".join(f"{e} {c}" for e, c in summary.items()) if summary else emoji
    _print(f"  Reacted {emoji} to [dim]{message_id[:12]}[/]  ({rx_str})")
    _print("")


@main.command(name="reactions")
@click.argument("message_id")
def reactions_cmd(message_id: str) -> None:
    """Show all reactions for a message.

    Examples:

        skchat reactions msg-abc123
    """
    summary = _reaction_store.get_summary(message_id)
    reactions = _reaction_store.get(message_id)

    _print("")
    if not summary:
        _print(f"  [dim]No reactions for {message_id[:12]}[/]")
        _print("")
        return

    if HAS_RICH and console:
        from rich.table import Table as _Table

        tbl = _Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        tbl.add_column("Emoji", style="bold")
        tbl.add_column("Count", style="cyan")
        tbl.add_column("Senders", style="dim")

        grouped: dict[str, list[str]] = {}
        for r in reactions:
            grouped.setdefault(r.emoji, []).append(r.sender)

        for emoji_key, senders in grouped.items():
            tbl.add_row(emoji_key, str(len(senders)), ", ".join(senders))

        console.print(tbl)
    else:
        for emoji_key, count in summary.items():
            click.echo(f"  {emoji_key}  {count}")

    _print("")


@main.command()
@click.option("--limit", "-n", default=20, help="Max rooms per section (default: 20).")
def rooms(limit: int) -> None:
    """List group rooms and DM threads with last message and unread count.

    Groups are deduplicated by name (most recent kept).
    DM threads are derived from chat history.
    Unread count = messages since last ``skchat inbox --unread`` view.

    Examples:

        skchat rooms

        skchat rooms --limit 10
    """
    my_identity = _get_identity()

    # ── Read state for unread calculation ────────────────────────
    read_state = _load_read_state()
    last_read_str = read_state.get("_global", "")
    last_read_dt: Optional[datetime] = None
    if last_read_str:
        try:
            last_read_dt = datetime.fromisoformat(last_read_str)
            if last_read_dt.tzinfo is None:
                last_read_dt = last_read_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    def _is_unread(ts_raw: object) -> bool:
        if last_read_dt is None:
            return False
        try:
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            elif isinstance(ts_raw, datetime):
                ts = ts_raw
            else:
                return False
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts > last_read_dt
        except Exception:
            return False

    def _short_name(uri: str) -> str:
        return uri.split("@")[0].replace("capauth:", "").replace("nostr:", "").replace("group:", "grp/")[:20]

    def _msg_preview(msg: Optional[dict], width: int = 30) -> str:
        if msg is None:
            return ""
        content = str(msg.get("content") or "").replace("\n", " ").strip()
        if len(content) > width:
            return content[:width - 1] + "\u2026"
        return content

    # ── Scan history for last-msg + unread per group and DM ──────
    history = _get_history()
    group_last: dict[str, dict] = {}   # group_id -> last msg dict
    group_unread: dict[str, int] = {}  # group_id -> unread count
    dm_last: dict[str, dict] = {}      # peer_key -> last msg dict
    dm_unread: dict[str, int] = {}     # peer_key -> unread count
    dm_peer_uri: dict[str, str] = {}   # peer_key -> other peer URI
    try:
        all_mems = history._store.list_memories(tags=["skchat:message"], limit=2000)
        for m in all_mems:
            msg_dict = history._memory_to_chat_dict(m)
            sender = msg_dict.get("sender") or ""
            recipient = msg_dict.get("recipient") or ""
            ts = msg_dict.get("timestamp")
            ts_str = str(ts) if ts else ""

            if recipient.startswith("group:"):
                gid = recipient[len("group:"):]
                cur = group_last.get(gid)
                if cur is None or ts_str > str(cur.get("timestamp") or ""):
                    group_last[gid] = msg_dict
                if _is_unread(ts):
                    group_unread[gid] = group_unread.get(gid, 0) + 1
            else:
                # DM: determine the other party
                other = recipient if sender == my_identity else sender
                if not other or other == my_identity:
                    continue
                # Skip obviously non-person URIs (e.g. bare group IDs)
                if other.startswith("group:"):
                    continue
                pair = tuple(sorted([sender, recipient]))
                pk = f"{pair[0]}|{pair[1]}"
                cur = dm_last.get(pk)
                if cur is None or ts_str > str(cur.get("timestamp") or ""):
                    dm_last[pk] = msg_dict
                dm_peer_uri[pk] = other
                if _is_unread(ts):
                    dm_unread[pk] = dm_unread.get(pk, 0) + 1
    except Exception:
        pass

    # ── Load groups, deduplicate by name (keep latest updated_at) ─
    groups_dir = Path(SKCHAT_HOME).expanduser() / "groups"
    unique_groups: dict[str, object] = {}  # name -> GroupChat (latest)
    if groups_dir.exists():
        from .group import GroupChat
        for p in sorted(groups_dir.glob("*.json")):
            try:
                grp = GroupChat.model_validate_json(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            existing = unique_groups.get(grp.name)
            if existing is None or grp.updated_at > existing.updated_at:  # type: ignore[union-attr]
                unique_groups[grp.name] = grp

    entries = list(unique_groups.values())

    def _group_sort_key(g: object) -> str:
        msg = group_last.get(g.id)  # type: ignore[attr-defined]
        if msg:
            ts = msg.get("timestamp")
            if ts:
                return str(ts)
        return g.updated_at.isoformat()  # type: ignore[union-attr]

    entries.sort(key=_group_sort_key, reverse=True)
    entries = entries[:limit]

    # ── Build sorted DM list ──────────────────────────────────────
    dm_entries = [
        {
            "peer_uri": dm_peer_uri[pk],
            "last_msg": dm_last[pk],
            "unread": dm_unread.get(pk, 0),
        }
        for pk in dm_last
    ]
    dm_entries.sort(key=lambda x: str(x["last_msg"].get("timestamp") or ""), reverse=True)
    dm_entries = dm_entries[:limit]

    _print("")

    if HAS_RICH and console:
        # ── Groups table ─────────────────────────────────────────
        if entries:
            tbl = Table(
                title=f"[bold]Groups[/bold] ({len(entries)})",
                border_style="bright_blue",
                show_lines=False,
                padding=(0, 1),
            )
            tbl.add_column("Name", style="bold cyan", min_width=14, max_width=22)
            tbl.add_column("Mbrs", justify="right", max_width=4)
            tbl.add_column("Last message", max_width=32)
            tbl.add_column("When", style="dim", max_width=10)
            tbl.add_column("Unread", justify="right", max_width=6)
            for grp in entries:
                msg = group_last.get(grp.id)  # type: ignore[attr-defined]
                preview = _msg_preview(msg, 30)
                age = _ts_ago(msg.get("timestamp")) if msg else _ts_ago(grp.updated_at)  # type: ignore[union-attr]
                unread = group_unread.get(grp.id, 0)  # type: ignore[attr-defined]
                unread_cell = f"[bold red]{unread}[/bold red]" if unread else "[dim]-[/dim]"
                tbl.add_row(grp.name, str(grp.member_count), preview, age, unread_cell)  # type: ignore[attr-defined]
            console.print(tbl)
        else:
            console.print("  [dim]No groups found. Create one with: skchat group create <name>[/dim]")

        # ── DM threads table ─────────────────────────────────────
        if dm_entries:
            console.print("")
            tbl2 = Table(
                title="[bold]DM Threads[/bold]",
                border_style="magenta",
                show_lines=False,
                padding=(0, 1),
            )
            tbl2.add_column("Peer", style="cyan", min_width=12, max_width=22)
            tbl2.add_column("Last message", max_width=38)
            tbl2.add_column("When", style="dim", max_width=10)
            tbl2.add_column("Unread", justify="right", max_width=6)
            for dm in dm_entries:
                peer_display = _short_name(dm["peer_uri"])
                msg = dm["last_msg"]
                preview = _msg_preview(msg, 36)
                age = _ts_ago(msg.get("timestamp"))
                unread = dm["unread"]
                unread_cell = f"[bold red]{unread}[/bold red]" if unread else "[dim]-[/dim]"
                tbl2.add_row(peer_display, preview, age, unread_cell)
            console.print(tbl2)
        elif not entries:
            console.print("  [dim]No DM threads found.[/dim]")
    else:
        # Plain text fallback
        if entries:
            click.echo(f"\n  Groups ({len(entries)})")
            click.echo("  " + "-" * 72)
            click.echo(f"  {'Name':<22} {'M':>2}  {'Last message':<30}  {'When':<10}  Unread")
            click.echo("  " + "-" * 72)
            for grp in entries:
                msg = group_last.get(grp.id)  # type: ignore[attr-defined]
                preview = _msg_preview(msg, 28)
                age = _ts_ago(msg.get("timestamp")) if msg else _ts_ago(grp.updated_at)  # type: ignore[union-attr]
                unread = group_unread.get(grp.id, 0)  # type: ignore[attr-defined]
                click.echo(f"  {grp.name:<22} {grp.member_count:>2}  {preview:<30}  {age:<10}  {unread or '-'}")  # type: ignore[attr-defined]
        if dm_entries:
            click.echo(f"\n  DM Threads")
            click.echo("  " + "-" * 72)
            click.echo(f"  {'Peer':<22}  {'Last message':<36}  {'When':<10}  Unread")
            click.echo("  " + "-" * 72)
            for dm in dm_entries:
                peer_display = _short_name(dm["peer_uri"])
                msg = dm["last_msg"]
                preview = _msg_preview(msg, 34)
                age = _ts_ago(msg.get("timestamp"))
                unread = dm["unread"]
                click.echo(f"  {peer_display:<22}  {preview:<36}  {age:<10}  {unread or '-'}")
        if not entries and not dm_entries:
            click.echo("\n  No rooms or DM threads found.\n")

    _print("")


@main.command()
def tui() -> None:
    """Launch the interactive terminal UI (requires textual).

    Opens a full-screen chat interface with colour-coded messages,
    3-second inbox polling, @mention tab-completion, and Ctrl+G group mode.

    Examples:

        skchat tui
    """
    try:
        from .tui import main as tui_main
    except ImportError:
        _print(
            "\n  [red]Error:[/] textual is not installed.\n"
            "  Install with: [cyan]pip install textual[/]\n"
        )
        raise SystemExit(1)
    tui_main()


@main.command()
@click.option("--port", "-p", default=8765, show_default=True, help="TCP port to listen on.")
@click.option("--no-browser", is_flag=True, default=False, help="Do not open browser automatically.")
def webui(port: int, no_browser: bool) -> None:
    """Launch the web-based chat UI (requires fastapi + uvicorn).

    Opens a browser window at http://localhost:<port> with an HTMX chat
    interface that polls for new messages every 3 seconds.

    Examples:

        skchat webui
        skchat webui --port 9000 --no-browser
    """
    try:
        from .webui import run as webui_run
    except ImportError:
        _print(
            "\n  [red]Error:[/] fastapi/uvicorn is not installed.\n"
            "  Install with: [cyan]pip install fastapi uvicorn[/]\n"
        )
        raise SystemExit(1)
    webui_run(port=port, open_browser=not no_browser)


if __name__ == "__main__":
    main()
