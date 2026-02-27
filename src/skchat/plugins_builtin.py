"""Built-in SKChat plugins — shipped with skchat core.

These plugins demonstrate the plugin SDK and provide essential features:
1. LinkPreview — extracts and previews URLs in messages
2. CodeFormat — syntax-highlights code blocks in messages
3. EphemeralHelper — /burn slash command for quick ephemeral messages
4. ReactShortcut — /react slash command for quick reactions
5. StatusPlugin — /status slash command showing chat health
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from .models import ChatMessage
from .plugins import ChatPlugin


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
        r'```(\w+)?\s*\n(.*?)```',
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
    """Return all built-in plugin instances.

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
