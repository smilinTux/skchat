"""SKChat CLI — sovereign encrypted chat from your terminal.

Commands:
    skchat send <recipient> <message>
    skchat inbox [--limit N]
    skchat history <participant> [--limit N]
    skchat threads [--limit N]
    skchat search <query>
    skchat status

All commands operate against the local SKMemory-backed chat history.
Messages are composed locally, stored via ChatHistory, and (when
transport is wired) sent via SKComm.
"""

from __future__ import annotations

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
from .models import ChatMessage, ContentType, DeliveryStatus, Thread
from .identity_bridge import (
    get_sovereign_identity,
    resolve_peer_name,
    PeerResolutionError,
)


SKCHAT_HOME = "~/.skchat"

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
@click.argument("message")
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
def send(
    recipient: str,
    message: str,
    thread: Optional[str],
    reply_to: Optional[str],
    ttl: Optional[int],
    ctype: str,
) -> None:
    """Send a message to a recipient.

    Composes a ChatMessage, stores it in local history, and
    (when transport is available) queues it for delivery via SKComm.

    The recipient can be either a full capauth URI or a friendly peer name
    that will be resolved from the peer registry (e.g., "lumina" resolves
    to "capauth:lumina@capauth.local").

    Examples:

        skchat send capauth:bob@skworld.io "Hey Bob!"

        skchat send lumina "Check this out" --thread abc123

        skchat send bob "Secret" --ttl 60
    """
    sender = _get_identity()
    
    try:
        resolved_recipient = resolve_peer_name(recipient)
    except PeerResolutionError as exc:
        _print(f"\n  [red]Error:[/] {exc}")
        _print(f"  [yellow]Hint:[/] Using '{recipient}' as-is. Add peer with: skcapstone peer add {recipient}\n")
        resolved_recipient = recipient
    
    content_type = ContentType.PLAIN if ctype == "plain" else ContentType.MARKDOWN

    msg = ChatMessage(
        sender=sender,
        recipient=resolved_recipient,
        content=message,
        content_type=content_type,
        thread_id=thread,
        reply_to=reply_to,
        ttl=ttl,
        delivery_status=DeliveryStatus.PENDING,
    )

    history = _get_history()
    mem_id = history.store_message(msg)

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


@main.command()
@click.option("--limit", "-n", default=20, help="Max messages to show (default: 20).")
@click.option("--thread", "-t", default=None, help="Filter by thread ID.")
def inbox(limit: int, thread: Optional[str]) -> None:
    """Show recent incoming messages.

    Displays messages where the local user is the recipient,
    sorted newest first.

    Examples:

        skchat inbox

        skchat inbox --limit 5

        skchat inbox --thread abc123
    """
    identity = _get_identity()
    history = _get_history()

    if thread:
        messages = history.get_thread_messages(thread, limit=limit)
    else:
        messages = history.search_messages(identity, limit=limit)
        if not messages:
            all_msgs = history._store.list_memories(
                tags=["skchat:message"],
                limit=limit,
            )
            messages = [
                history._memory_to_chat_dict(m)
                for m in all_msgs
                if "skchat:message" in m.tags
            ]

    _print("")
    if not messages:
        _print("  [dim]No messages found.[/]")
        _print("")
        return

    if HAS_RICH and console:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"Inbox ({len(messages)} message{'s' if len(messages) != 1 else ''})",
        )
        table.add_column("From", style="cyan", max_width=30)
        table.add_column("Content", max_width=50)
        table.add_column("Thread", style="dim", max_width=12)
        table.add_column("Time", style="dim", max_width=20)

        for msg in messages:
            sender = msg.get("sender", "unknown")
            content = msg.get("content", "")
            preview = content[:50] + ("..." if len(content) > 50 else "")
            tid = (msg.get("thread_id") or "")[:12]
            ts = msg.get("timestamp", "")
            if isinstance(ts, str) and len(ts) > 19:
                ts = ts[:19]
            table.add_row(sender, preview, tid, str(ts))

        console.print(table)
    else:
        _print(f"  {len(messages)} message(s):")
        for msg in messages:
            sender = msg.get("sender", "unknown")
            content = msg.get("content", "")[:60]
            _print(f"    {sender}: {content}")

    _print("")


@main.command()
@click.argument("participant")
@click.option("--limit", "-n", default=30, help="Max messages to show (default: 30).")
def history(participant: str, limit: int) -> None:
    """Show conversation history with a participant.

    Displays the message exchange between you and the specified
    participant, sorted newest first.

    The participant can be either a full capauth URI or a friendly peer name
    that will be resolved from the peer registry.

    Examples:

        skchat history capauth:bob@skworld.io

        skchat history lumina --limit 10

        skchat history jarvis
    """
    identity = _get_identity()
    
    try:
        resolved_participant = resolve_peer_name(participant)
    except PeerResolutionError:
        resolved_participant = participant
    
    chat_history = _get_history()
    messages = chat_history.get_conversation(identity, resolved_participant, limit=limit)

    _print("")
    if not messages:
        _print(f"  [dim]No conversation history with {participant}.[/]")
        _print("")
        return

    if HAS_RICH and console:
        display_participant = participant if participant == resolved_participant else f"{participant} ({resolved_participant})"
        console.print(Panel(
            f"Conversation with [bold cyan]{display_participant}[/]\n"
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
def search(query: str, limit: int) -> None:
    """Search chat messages by content.

    Full-text search across all stored messages.

    Examples:

        skchat search "quantum upgrade"

        skchat search "deploy" --limit 5
    """
    chat_history = _get_history()
    results = chat_history.search_messages(query, limit=limit)

    _print("")
    if not results:
        _print(f"  [dim]No messages matching '{query}'.[/]")
        _print("")
        return

    if HAS_RICH and console:
        console.print(f"  [bold]{len(results)}[/] result{'s' if len(results) != 1 else ''} "
                       f"for [cyan]'{query}'[/]:\n")

        for msg in results:
            sender = msg.get("sender", "unknown")
            content = msg.get("content", "")
            preview = content[:80] + ("..." if len(content) > 80 else "")
            ts = msg.get("timestamp", "")
            if isinstance(ts, str) and len(ts) > 19:
                ts = ts[:19]
            console.print(f"  [cyan]{sender}[/] [dim]{ts}[/]")
            console.print(f"    {preview}\n")
    else:
        for msg in results:
            sender = msg.get("sender", "unknown")
            content = msg.get("content", "")[:60]
            _print(f"  {sender}: {content}")

    _print("")


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
@click.option("--interval", "-i", type=float, default=5.0, help="Poll interval in seconds (default: 5).")
@click.option("--limit", "-n", default=20, help="Max messages to show per poll.")
def watch(interval: float, limit: int) -> None:
    """Watch for incoming messages in real-time.

    Continuously polls SKComm for new messages and displays
    them as they arrive using Rich Live. Press Ctrl+C to stop.

    Examples:

        skchat watch

        skchat watch --interval 2

        skchat watch -i 10 -n 50
    """
    transport = _get_transport()
    if transport is None:
        _print("\n  [yellow]No transport available.[/] Configure SKComm first.\n")
        return

    history = _get_history()
    identity = _get_identity()

    _print(f"\n  [cyan]Watching for messages...[/] (poll every {interval}s, Ctrl+C to stop)\n")

    total_received = 0

    if HAS_RICH and console:
        try:
            from rich.live import Live

            table = _build_watch_table([], total_received)

            with Live(table, console=console, refresh_per_second=0.5) as live:
                import time

                while True:
                    try:
                        messages = transport.poll_inbox()
                        if messages:
                            total_received += len(messages)
                            table = _build_watch_table(messages, total_received)
                            live.update(table)
                    except Exception as exc:
                        live.update(Panel(f"[red]Poll error: {exc}[/]", border_style="red"))
                    time.sleep(interval)
        except KeyboardInterrupt:
            _print(f"\n  [dim]Stopped. {total_received} message(s) received total.[/]\n")
        except ImportError:
            _print("  [yellow]Rich Live not available. Use 'skchat receive' for one-shot poll.[/]\n")
    else:
        import time

        try:
            while True:
                messages = transport.poll_inbox()
                if messages:
                    total_received += len(messages)
                    for msg in messages:
                        _print(f"  [{msg.sender}] {msg.content[:80]}")
                time.sleep(interval)
        except KeyboardInterrupt:
            _print(f"\n  Stopped. {total_received} message(s) received total.\n")


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
    """Persist a GroupChat by storing its full state in thread metadata.

    The GroupChat is serialized into the Thread's metadata dict under
    the ``group_data`` key so it can be fully reconstructed later.

    Args:
        grp: The GroupChat to persist.

    Returns:
        str: The memory ID assigned to this group record.
    """
    history = _get_history()
    thread = grp.to_thread()
    thread.metadata["group_data"] = grp.model_dump(mode="json")
    return history.store_thread(thread)


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
    thread_data = history.get_thread(group_id)
    if thread_data is None:
        return None

    group_data = thread_data.get("group_data")
    if group_data is None:
        return None

    return GroupChat.model_validate(group_data)


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
    """Send a message to a group.

    Examples:

        skchat group send abc123 "Hello team!"

        skchat group send abc123 "Secret" --ttl 60
    """
    from .models import ChatMessage, ContentType

    identity = _get_identity()
    msg = ChatMessage(
        sender=identity,
        recipient=f"group:{group_id}",
        content=message,
        content_type=ContentType.MARKDOWN,
        thread_id=group_id,
        ttl=ttl,
        metadata={"group_message": True},
    )

    history = _get_history()
    mem_id = history.store_message(msg)

    transport_info = _try_deliver(msg)

    _print("")
    if transport_info.get("delivered"):
        _print(f"  [green]Sent to group {group_id[:12]}[/] via {transport_info.get('transport')}")
    else:
        _print(f"  [yellow]Stored locally[/] ({transport_info.get('error', 'no transport')})")
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
        table.add_column("Scope", style="dim", max_width=30)

        for m in grp.members:
            role_style = "green" if m.role.value == "admin" else ""
            scope_str = ", ".join(m.tool_scope) if m.tool_scope else "unrestricted"
            table.add_row(
                m.display_name,
                m.identity_uri,
                Text(m.role.value, style=role_style),
                m.participant_type.value,
                scope_str,
            )

        console.print(table)
    else:
        for m in grp.members:
            scope_str = ", ".join(m.tool_scope) if m.tool_scope else "unrestricted"
            _print(f"  {m.display_name} ({m.role.value}, {m.participant_type.value}) scope={scope_str}")
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
def status() -> None:
    """Show SKChat status and statistics.

    Displays local identity, message count, thread count,
    and storage health.
    """
    identity = _get_identity()
    chat_history = _get_history()
    msg_count = chat_history.message_count()
    thread_list = chat_history.list_threads(limit=1000)

    _print("")
    if HAS_RICH and console:
        console.print(Panel(
            f"[bold]Identity:[/] [cyan]{identity}[/]\n"
            f"[bold]Messages:[/] {msg_count}\n"
            f"[bold]Threads:[/] {len(thread_list)}\n"
            f"[bold]Storage:[/] [green]SKMemory[/]\n"
            f"[bold]Version:[/] {__version__}",
            title="SKChat Status",
            border_style="bright_blue",
        ))
    else:
        _print(f"  Identity: {identity}")
        _print(f"  Messages: {msg_count}")
        _print(f"  Threads: {len(thread_list)}")
        _print(f"  Version: {__version__}")
    _print("")


if __name__ == "__main__":
    main()
