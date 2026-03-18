"""Desktop notification support for SKChat.

Wraps notify-send with urgency levels and icon selection.
Silently no-ops when notify-send is not available.
"""

from __future__ import annotations

import subprocess
from enum import Enum


class NotificationLevel(Enum):
    LOW = "low"
    NORMAL = "normal"
    CRITICAL = "critical"


class DesktopNotifier:
    """Send desktop notifications via notify-send.

    Args:
        app_name: Application name shown in notification header.
    """

    def __init__(self, app_name: str = "SKChat") -> None:
        self.app_name = app_name
        self.available = self._check_available()

    def _check_available(self) -> bool:
        return subprocess.run(["which", "notify-send"], capture_output=True).returncode == 0

    def notify(
        self,
        title: str,
        body: str,
        urgency: str = "normal",
        icon: str = "dialog-information",
        timeout_ms: int = 5000,
    ) -> bool:
        """Send a desktop notification.

        Args:
            title: Notification title.
            body: Notification body text.
            urgency: notify-send urgency level (low, normal, critical).
            icon: Icon name or path passed to --icon.
            timeout_ms: Auto-dismiss timeout in milliseconds.

        Returns:
            True if notify-send exited successfully, False otherwise.
        """
        if not self.available:
            return False
        cmd = [
            "notify-send",
            "--urgency",
            urgency,
            "--icon",
            icon,
            "--expire-time",
            str(timeout_ms),
            title,
            body,
        ]
        return subprocess.run(cmd, capture_output=True).returncode == 0

    def notify_message(
        self,
        sender_name: str,
        preview: str,
        is_mention: bool = False,
    ) -> bool:
        """Notify about an incoming chat message.

        Args:
            sender_name: Short sender name (no domain).
            preview: Truncated message preview.
            is_mention: If True, use critical urgency.

        Returns:
            True if the notification was sent successfully.
        """
        urgency = (
            NotificationLevel.CRITICAL.value if is_mention else NotificationLevel.NORMAL.value
        )
        return self.notify(
            title=f"SKChat: {sender_name}",
            body=preview,
            urgency=urgency,
            icon="dialog-information",
        )

    def notify_lumina(self, preview: str) -> bool:
        """Special notification for Lumina messages.

        Args:
            preview: Truncated message preview.

        Returns:
            True if the notification was sent successfully.
        """
        return self.notify(
            title="\U0001f49c Lumina",
            body=preview,
            urgency=NotificationLevel.NORMAL.value,
            timeout_ms=10000,
        )
