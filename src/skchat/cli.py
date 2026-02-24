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


def _try_deliver(msg) -> dict:
    """Attempt to deliver a message via SKComm transport.

    Args:
        msg: ChatMessage to deliver.

    Returns:
        dict: Delivery result with 'delivered', 'transport', and 'error' keys.
    """
    transport = _get_chat_transport()
    if transport is None:
        return {"delivered": False, "error": "no transport configured"}

    result = transport.send_message(msg)
    return {
        "delivered": result.get("delivered", False),
        "transport": result.get("transport"),
        "error": result.get("error"),
        "envelope_id": None,
    }


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


@main.command()
@click.option("--interval", "-i", type=float, default=5.0, help="Poll interval in seconds (default: 5).")
@click.option("--log-file", "-l", default=None, help="Path to log file (default: stdout).")
@click.option("--quiet", "-q", is_flag=True, help="Suppress console output, log to file only.")
def daemon(interval: float, log_file: Optional[str], quiet: bool) -> None:
    """Run receive daemon as a background service.

    Continuously polls SKComm for incoming messages and stores them
    in local history. Runs until stopped with Ctrl+C or SIGTERM.

    For production use, consider running as a systemd service or
    in a tmux/screen session.

    Examples:

        skchat daemon

        skchat daemon --interval 10 --log-file ~/.skchat/daemon.log

        skchat daemon --quiet --log-file /var/log/skchat.log
    """
    try:
        from .daemon import run_daemon
        run_daemon(interval=interval, log_file=log_file, quiet=quiet)
    except ImportError:
        _print("\n  [red]Error:[/] Daemon module not available.\n")
        sys.exit(1)
    except KeyboardInterrupt:
        _print("\n  [dim]Daemon stopped.[/]\n")
    except Exception as exc:
        _print(f"\n  [red]Error:[/] {exc}\n")
        sys.exit(1)


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
