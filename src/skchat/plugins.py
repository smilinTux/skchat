"""SKChat plugin system — extensible message processing and UI hooks.

Plugins extend SKChat with custom behavior: message formatting, slash
commands, content handlers, notification filters, and more. Each plugin
is a Python class that implements the ChatPlugin interface.

Discovery:
    1. Built-in plugins in skchat.plugins.builtins/
    2. Installed packages with 'skchat.plugins' entry point group
    3. Python files in ~/.skchat/plugins/

Lifecycle:
    load -> activate -> (hooks called during runtime) -> deactivate -> unload

Usage:
    registry = PluginRegistry()
    registry.discover()
    registry.activate_all()

    # Plugins get called on message events
    for plugin in registry.active_plugins:
        msg = plugin.on_outbound(msg)
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .models import ChatMessage

logger = logging.getLogger("skchat.plugins")


class PluginState(str, Enum):
    """Lifecycle state of a plugin."""

    DISCOVERED = "discovered"
    LOADED = "loaded"
    ACTIVE = "active"
    ERROR = "error"
    DISABLED = "disabled"


class PluginMeta(BaseModel):
    """Metadata about a registered plugin.

    Attributes:
        name: Unique plugin identifier.
        version: Semver version string.
        description: Human-readable description.
        author: Plugin author.
        source: Where the plugin was discovered (builtin, entrypoint, user).
        state: Current lifecycle state.
        error: Last error message, if in error state.
    """

    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    source: str = "unknown"
    state: PluginState = PluginState.DISCOVERED
    error: Optional[str] = None


class ChatPlugin(ABC):
    """Base class for all SKChat plugins.

    Implement the hooks you need. Unimplemented hooks are no-ops.

    Attributes:
        name: Unique plugin name (e.g., "skseal", "code-highlight").
        version: Semver version string.
        description: What this plugin does.
        author: Who wrote it.
    """

    name: str = "unnamed"
    version: str = "0.1.0"
    description: str = ""
    author: str = ""

    def activate(self) -> None:
        """Called when the plugin is activated. Initialize resources here."""

    def deactivate(self) -> None:
        """Called when the plugin is deactivated. Clean up resources here."""

    def on_outbound(self, message: ChatMessage) -> ChatMessage:
        """Process an outgoing message before it's sent.

        Can modify content, add metadata, or block sending by raising.

        Args:
            message: The outgoing ChatMessage.

        Returns:
            ChatMessage: Possibly modified message.
        """
        return message

    def on_inbound(self, message: ChatMessage) -> ChatMessage:
        """Process an incoming message before it's displayed.

        Can modify content, add metadata, filter, or enrich.

        Args:
            message: The incoming ChatMessage.

        Returns:
            ChatMessage: Possibly modified message.
        """
        return message

    def on_command(self, command: str, args: str, context: dict) -> Optional[str]:
        """Handle a slash command (e.g., /sign, /encrypt, /format).

        Args:
            command: The command name (without the slash).
            args: The argument string after the command.
            context: Dict with 'sender', 'recipient', 'thread_id'.

        Returns:
            Optional[str]: Response text, or None if not handled.
        """
        return None

    @property
    def commands(self) -> list[str]:
        """List of slash commands this plugin handles.

        Returns:
            list[str]: Command names (without slash prefix).
        """
        return []

    def meta(self) -> PluginMeta:
        """Get plugin metadata.

        Returns:
            PluginMeta: Metadata about this plugin.
        """
        return PluginMeta(
            name=self.name,
            version=self.version,
            description=self.description,
            author=self.author,
        )


class PluginRegistry:
    """Discovers, loads, and manages SKChat plugins.

    Supports three plugin sources:
    1. Built-in plugins from skchat source
    2. Installed packages with 'skchat.plugins' entry points
    3. User plugins from ~/.skchat/plugins/*.py

    Args:
        user_plugin_dir: Override user plugin directory.
    """

    ENTRY_POINT_GROUP = "skchat.plugins"

    def __init__(self, user_plugin_dir: Optional[Path] = None) -> None:
        self._plugins: dict[str, ChatPlugin] = {}
        self._meta: dict[str, PluginMeta] = {}
        self._user_dir = user_plugin_dir or Path("~/.skchat/plugins").expanduser()

    @property
    def all_plugins(self) -> dict[str, PluginMeta]:
        """All registered plugins with their metadata."""
        return dict(self._meta)

    @property
    def active_plugins(self) -> list[ChatPlugin]:
        """All plugins in ACTIVE state, sorted by name."""
        return sorted(
            [p for p in self._plugins.values() if self._meta[p.name].state == PluginState.ACTIVE],
            key=lambda p: p.name,
        )

    def discover(self) -> int:
        """Discover plugins from all sources.

        Returns:
            int: Number of plugins discovered.
        """
        count = 0
        count += self._discover_builtins()
        count += self._discover_entrypoints()
        count += self._discover_user_plugins()
        logger.info("Discovered %d plugin(s)", count)
        return count

    def register(self, plugin: ChatPlugin, source: str = "manual") -> bool:
        """Register a plugin instance.

        Args:
            plugin: The ChatPlugin to register.
            source: Where the plugin came from.

        Returns:
            bool: True if registered (not a duplicate).
        """
        if plugin.name in self._plugins:
            logger.debug("Plugin '%s' already registered — skipping", plugin.name)
            return False

        self._plugins[plugin.name] = plugin
        meta = plugin.meta()
        meta.source = source
        meta.state = PluginState.LOADED
        self._meta[plugin.name] = meta

        logger.info("Registered plugin '%s' v%s from %s", plugin.name, plugin.version, source)
        return True

    def activate(self, name: str) -> bool:
        """Activate a loaded plugin.

        Args:
            name: Plugin name to activate.

        Returns:
            bool: True if activated successfully.
        """
        plugin = self._plugins.get(name)
        meta = self._meta.get(name)
        if not plugin or not meta:
            return False

        if meta.state == PluginState.ACTIVE:
            return True

        try:
            plugin.activate()
            meta.state = PluginState.ACTIVE
            meta.error = None
            logger.info("Activated plugin '%s'", name)
            return True
        except Exception as exc:
            meta.state = PluginState.ERROR
            meta.error = str(exc)
            logger.warning("Failed to activate plugin '%s': %s", name, exc)
            return False

    def deactivate(self, name: str) -> bool:
        """Deactivate an active plugin.

        Args:
            name: Plugin name to deactivate.

        Returns:
            bool: True if deactivated successfully.
        """
        plugin = self._plugins.get(name)
        meta = self._meta.get(name)
        if not plugin or not meta:
            return False

        if meta.state != PluginState.ACTIVE:
            return True

        try:
            plugin.deactivate()
        except Exception as exc:
            logger.warning("Error deactivating plugin '%s': %s", name, exc)

        meta.state = PluginState.LOADED
        return True

    def activate_all(self) -> int:
        """Activate all loaded plugins.

        Returns:
            int: Number of plugins activated.
        """
        count = 0
        for name in list(self._meta.keys()):
            if self.activate(name):
                count += 1
        return count

    def deactivate_all(self) -> None:
        """Deactivate all active plugins."""
        for name in list(self._meta.keys()):
            self.deactivate(name)

    def process_outbound(self, message: ChatMessage) -> ChatMessage:
        """Run all active plugins' outbound hooks.

        Args:
            message: The outgoing message.

        Returns:
            ChatMessage: Message after all plugins have processed it.
        """
        for plugin in self.active_plugins:
            try:
                message = plugin.on_outbound(message)
            except Exception as exc:
                logger.warning("Plugin '%s' outbound hook failed: %s", plugin.name, exc)
        return message

    def process_inbound(self, message: ChatMessage) -> ChatMessage:
        """Run all active plugins' inbound hooks.

        Args:
            message: The incoming message.

        Returns:
            ChatMessage: Message after all plugins have processed it.
        """
        for plugin in self.active_plugins:
            try:
                message = plugin.on_inbound(message)
            except Exception as exc:
                logger.warning("Plugin '%s' inbound hook failed: %s", plugin.name, exc)
        return message

    def handle_command(self, command: str, args: str, context: dict) -> Optional[str]:
        """Route a slash command to the appropriate plugin.

        Args:
            command: Command name (without slash).
            args: Argument string.
            context: Dict with sender, recipient, thread_id.

        Returns:
            Optional[str]: Response from the handling plugin, or None.
        """
        for plugin in self.active_plugins:
            if command in plugin.commands:
                try:
                    return plugin.on_command(command, args, context)
                except Exception as exc:
                    logger.warning("Plugin '%s' command handler failed: %s", plugin.name, exc)
                    return f"Error in plugin '{plugin.name}': {exc}"
        return None

    def list_commands(self) -> dict[str, str]:
        """Get all available slash commands from active plugins.

        Returns:
            dict: command_name -> plugin_name.
        """
        cmds: dict[str, str] = {}
        for plugin in self.active_plugins:
            for cmd in plugin.commands:
                cmds[cmd] = plugin.name
        return cmds

    def _discover_builtins(self) -> int:
        """Load built-in plugins shipped with skchat."""
        count = 0
        try:
            from .plugins_builtin import get_builtin_plugins

            for plugin in get_builtin_plugins():
                if self.register(plugin, source="builtin"):
                    count += 1
        except ImportError:
            pass
        return count

    def _discover_entrypoints(self) -> int:
        """Load plugins from installed packages via entry points."""
        count = 0
        try:
            eps = importlib.metadata.entry_points()
            group_eps = eps.select(group=self.ENTRY_POINT_GROUP) if hasattr(eps, "select") else []
            for ep in group_eps:
                try:
                    plugin_cls = ep.load()
                    plugin = plugin_cls()
                    if self.register(plugin, source=f"entrypoint:{ep.name}"):
                        count += 1
                except Exception as exc:
                    logger.warning("Failed to load entry point '%s': %s", ep.name, exc)
        except Exception:
            pass
        return count

    def _discover_user_plugins(self) -> int:
        """Load plugins from ~/.skchat/plugins/*.py."""
        count = 0
        if not self._user_dir.exists():
            return count

        for py_file in sorted(self._user_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"skchat_user_plugin_{py_file.stem}", py_file
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    plugin_cls = getattr(module, "Plugin", None)
                    if plugin_cls and issubclass(plugin_cls, ChatPlugin):
                        plugin = plugin_cls()
                        if self.register(plugin, source=f"user:{py_file.name}"):
                            count += 1
                    else:
                        logger.debug("No Plugin class in %s", py_file.name)
            except Exception as exc:
                logger.warning("Failed to load user plugin '%s': %s", py_file.name, exc)
        return count
