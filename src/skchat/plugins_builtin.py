"""Built-in SKChat plugins — shipped with skchat core.

Slash-command plugins (ChatPlugin subclasses):
1. LinkPreview — extracts and previews URLs in messages
2. CodeFormat — syntax-highlights code blocks in messages
3. EphemeralHelper — /burn slash command for quick ephemeral messages
4. ReactShortcut — /react slash command for quick reactions
5. StatusPlugin — /status slash command showing chat health

Trigger-based plugins (SKChatPlugin subclasses):
6. EchoPlugin — responds to "echo: MESSAGE" with MESSAGE (testing)
7. DaemonStatusPlugin — responds to "!status" with daemon status info
8. TranslatePlugin — responds to "!translate LANG: TEXT" via translate-shell
9. WeatherPlugin — responds to "!weather CITY" via wttr.in (curl)
10. TimePlugin — responds to "!time" with current time/timezone info
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

from .models import ChatMessage
from .plugins import ChatPlugin, SKChatPlugin


class LinkPreviewPlugin(ChatPlugin):
    """Detects URLs in messages and adds metadata for rich previews.

    On outbound: scans content for URLs and stores them in metadata
    so the UI layer can render link previews.

    Commands: none
    """

    name = "link-preview"
    version = "0.1.0"
    description = "Detect URLs in messages and add preview metadata"
    author = "smilinTux"

    URL_PATTERN = re.compile(
        r'https?://[^\s<>"\')\]]+',
        re.IGNORECASE,
    )

    def on_outbound(self, message: ChatMessage) -> ChatMessage:
        urls = self.URL_PATTERN.findall(message.content)
        if urls:
            metadata = dict(message.metadata)
            metadata["detected_urls"] = urls[:5]
            return message.model_copy(update={"metadata": metadata})
        return message

    def on_inbound(self, message: ChatMessage) -> ChatMessage:
        urls = self.URL_PATTERN.findall(message.content)
        if urls:
            metadata = dict(message.metadata)
            metadata["detected_urls"] = urls[:5]
            return message.model_copy(update={"metadata": metadata})
        return message


class CodeFormatPlugin(ChatPlugin):
    """Detects code blocks in messages and adds formatting metadata.

    Scans for markdown fenced code blocks (```lang ... ```) and
    adds language hints to metadata for syntax highlighting.

    Commands: none
    """

    name = "code-format"
    version = "0.1.0"
    description = "Detect code blocks and add language metadata for highlighting"
    author = "smilinTux"

    CODE_BLOCK_PATTERN = re.compile(
        r"```(\w+)?\s*\n(.*?)```",
        re.DOTALL,
    )

    def on_inbound(self, message: ChatMessage) -> ChatMessage:
        blocks = self.CODE_BLOCK_PATTERN.findall(message.content)
        if blocks:
            langs = [lang for lang, _ in blocks if lang]
            if langs:
                metadata = dict(message.metadata)
                metadata["code_languages"] = langs
                metadata["has_code"] = True
                return message.model_copy(update={"metadata": metadata})
        return message


class EphemeralHelperPlugin(ChatPlugin):
    """Provides /burn command for quick ephemeral (self-destructing) messages.

    Usage: /burn 60 This message self-destructs in 60 seconds

    Commands: burn
    """

    name = "ephemeral-helper"
    version = "0.1.0"
    description = "/burn command for quick self-destructing messages"
    author = "smilinTux"

    @property
    def commands(self) -> list[str]:
        return ["burn"]

    def on_command(self, command: str, args: str, context: dict) -> Optional[str]:
        if command != "burn":
            return None

        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            return "Usage: /burn <seconds> <message>"

        try:
            ttl = int(parts[0])
        except ValueError:
            return f"Invalid TTL: '{parts[0]}'. Must be a number of seconds."

        if ttl < 1 or ttl > 86400:
            return "TTL must be between 1 and 86400 seconds (24 hours)."

        return f"__ephemeral__:{ttl}:{parts[1]}"


class ReactShortcutPlugin(ChatPlugin):
    """Provides /react command for quick message reactions.

    Usage: /react <message_id> <emoji>

    Commands: react
    """

    name = "react-shortcut"
    version = "0.1.0"
    description = "/react command for quick message reactions"
    author = "smilinTux"

    @property
    def commands(self) -> list[str]:
        return ["react"]

    def on_command(self, command: str, args: str, context: dict) -> Optional[str]:
        if command != "react":
            return None

        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            return "Usage: /react <message_id> <emoji>"

        message_id = parts[0]
        emoji = parts[1].strip()

        sender = context.get("sender", "unknown")
        return f"__reaction__:{message_id}:{emoji}:{sender}"


class StatusPlugin(ChatPlugin):
    """Provides /status and /whoami slash commands.

    /status  — shows daemon status, message count, thread count
    /whoami  — shows current identity URI

    Commands: status, whoami
    """

    name = "status"
    version = "0.1.0"
    description = "/status and /whoami commands for chat health"
    author = "smilinTux"

    @property
    def commands(self) -> list[str]:
        return ["status", "whoami"]

    def on_command(self, command: str, args: str, context: dict) -> Optional[str]:
        if command == "whoami":
            return f"Identity: {context.get('sender', 'unknown')}"

        if command == "status":
            sender = context.get("sender", "unknown")
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            return (
                f"SKChat Status\n"
                f"  Identity: {sender}\n"
                f"  Time: {now}\n"
                f"  Thread: {context.get('thread_id', 'none')}"
            )

        return None


def get_builtin_plugins() -> list[ChatPlugin]:
    """Return all built-in slash-command plugin instances.

    Returns:
        list[ChatPlugin]: All built-in plugins ready for registration.
    """
    return [
        LinkPreviewPlugin(),
        CodeFormatPlugin(),
        EphemeralHelperPlugin(),
        ReactShortcutPlugin(),
        StatusPlugin(),
    ]


# ---------------------------------------------------------------------------
# Trigger-based plugins (SKChatPlugin subclasses)
# ---------------------------------------------------------------------------

_ECHO_PATTERN = re.compile(r"^echo:\s+(.+)$", re.IGNORECASE | re.DOTALL)
_TRANSLATE_PATTERN = re.compile(r"^!translate\s+(\w+):\s+(.+)$", re.IGNORECASE | re.DOTALL)
_WEATHER_PATTERN = re.compile(r"^!weather\s+(.+)$", re.IGNORECASE)


class EchoPlugin(SKChatPlugin):
    """Responds to "echo: MESSAGE" with MESSAGE — useful for testing.

    Triggers: any message matching "echo: <text>"
    Reply: the text after "echo: "
    """

    name = "echo"
    triggers = ["echo:"]

    def should_handle(self, message: ChatMessage) -> bool:
        return bool(_ECHO_PATTERN.match(message.content.strip()))

    def handle(self, message: ChatMessage) -> Optional[str]:
        m = _ECHO_PATTERN.match(message.content.strip())
        if m:
            return m.group(1).strip()
        return None


class DaemonStatusPlugin(SKChatPlugin):
    """Responds to "!status" with current daemon runtime statistics.

    Triggers: message content is exactly "!status" (case-insensitive)
    Reply: formatted daemon status including uptime, message counts,
           transport health, and online peer count.
    """

    name = "daemon-status"
    triggers = ["!status"]

    def should_handle(self, message: ChatMessage) -> bool:
        return message.content.strip().lower() == "!status"

    def handle(self, message: ChatMessage) -> Optional[str]:
        try:
            from .daemon import daemon_status

            s = daemon_status()
        except Exception as exc:
            return f"Status unavailable: {exc}"

        running = s.get("running", False)
        uptime_s = s.get("uptime_seconds", 0)
        msgs_recv = s.get("messages_received", "n/a")
        msgs_sent = s.get("messages_sent", "n/a")
        transport = s.get("transport_status", "unknown")
        peers = s.get("online_peer_count", 0)

        if uptime_s and isinstance(uptime_s, (int, float)) and uptime_s > 0:
            h, rem = divmod(int(uptime_s), 3600)
            m, sec = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {sec}s" if h else (f"{m}m {sec}s" if m else f"{sec}s")
        else:
            uptime_str = "n/a"

        return (
            f"SKChat Daemon Status\n"
            f"  Running: {running}\n"
            f"  Uptime: {uptime_str}\n"
            f"  Messages recv/sent: {msgs_recv}/{msgs_sent}\n"
            f"  Transport: {transport}\n"
            f"  Online peers: {peers}"
        )


class TranslatePlugin(SKChatPlugin):
    """Translates text via translate-shell subprocess.

    Usage: !translate LANG: TEXT
    Example: !translate fr: Hello, world!
    Triggers: any message starting with "!translate"

    Requires translate-shell (trans) to be installed.
    """

    name = "translate"
    triggers = ["!translate"]

    def should_handle(self, message: ChatMessage) -> bool:
        return bool(_TRANSLATE_PATTERN.match(message.content.strip()))

    def handle(self, message: ChatMessage) -> Optional[str]:
        m = _TRANSLATE_PATTERN.match(message.content.strip())
        if not m:
            return "Usage: !translate LANG: TEXT  (e.g. !translate fr: Hello)"

        lang = m.group(1).strip()
        text = m.group(2).strip()

        try:
            result = subprocess.run(
                ["trans", "-brief", f":{lang}", text],
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = result.stdout.strip()
            if result.returncode != 0 or not output:
                err = result.stderr.strip() or "translation failed"
                return f"Translate error: {err}"
            return output
        except FileNotFoundError:
            return (
                "translate-shell (trans) is not installed. "
                "Install with: sudo pacman -S translate-shell"
            )
        except subprocess.TimeoutExpired:
            return "Translation timed out."
        except Exception as exc:
            return f"Translate error: {exc}"


class WeatherPlugin(SKChatPlugin):
    """Fetches current weather for a city via wttr.in (curl).

    Usage: !weather CITY
    Example: !weather London
    Triggers: any message starting with "!weather"
    """

    name = "weather"
    triggers = ["!weather"]

    def should_handle(self, message: ChatMessage) -> bool:
        return bool(_WEATHER_PATTERN.match(message.content.strip()))

    def handle(self, message: ChatMessage) -> Optional[str]:
        m = _WEATHER_PATTERN.match(message.content.strip())
        if not m:
            return "Usage: !weather CITY  (e.g. !weather Berlin)"

        city = m.group(1).strip().replace(" ", "+")

        try:
            result = subprocess.run(
                ["curl", "-s", f"wttr.in/{city}?format=3"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout.strip()
            if result.returncode != 0 or not output:
                return f"Weather unavailable for '{city}'."
            return output
        except FileNotFoundError:
            return "curl is not installed."
        except subprocess.TimeoutExpired:
            return "Weather request timed out."
        except Exception as exc:
            return f"Weather error: {exc}"


class TimePlugin(SKChatPlugin):
    """Responds to "!time" with the current UTC time and local timezone.

    Triggers: message content is exactly "!time" (case-insensitive)
    Reply: current UTC time plus local timezone offset.
    """

    name = "time"
    triggers = ["!time"]

    def should_handle(self, message: ChatMessage) -> bool:
        return message.content.strip().lower() == "!time"

    def handle(self, message: ChatMessage) -> Optional[str]:
        import time as _time

        now_utc = datetime.now(timezone.utc)
        utc_str = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

        try:
            local_tz_name = _time.tzname[0]
            local_now = datetime.now()
            local_str = local_now.strftime(f"%Y-%m-%d %H:%M:%S {local_tz_name}")
        except Exception:
            local_str = "n/a"

        return f"Current time:\n  UTC:   {utc_str}\n  Local: {local_str}"


def get_trigger_plugins() -> list[SKChatPlugin]:
    """Return all built-in trigger plugin instances.

    Returns:
        list[SKChatPlugin]: Trigger plugins ready for registration.
    """
    return [
        EchoPlugin(),
        DaemonStatusPlugin(),
        TranslatePlugin(),
        WeatherPlugin(),
        TimePlugin(),
    ]
