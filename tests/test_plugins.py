"""Tests for the SKChat plugin system."""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from skchat.models import ChatMessage, ContentType
from skchat.plugins import (
    ChatPlugin,
    PluginMeta,
    PluginRegistry,
    PluginState,
)
from skchat.plugins_builtin import (
    CodeFormatPlugin,
    EphemeralHelperPlugin,
    LinkPreviewPlugin,
    ReactShortcutPlugin,
    StatusPlugin,
    get_builtin_plugins,
)


def _msg(content="Hello world", sender="capauth:alice@test", recipient="capauth:bob@test"):
    return ChatMessage(sender=sender, recipient=recipient, content=content)


class TestPluginMeta:
    def test_default_state(self):
        meta = PluginMeta(name="test")
        assert meta.state == PluginState.DISCOVERED
        assert meta.error is None

    def test_full_meta(self):
        meta = PluginMeta(
            name="test",
            version="1.0.0",
            description="A test plugin",
            author="tester",
            source="manual",
        )
        assert meta.name == "test"
        assert meta.version == "1.0.0"


class TestChatPluginBase:
    def test_default_hooks_are_noop(self):
        class MinimalPlugin(ChatPlugin):
            name = "minimal"

        plugin = MinimalPlugin()
        msg = _msg()
        assert plugin.on_outbound(msg) is msg
        assert plugin.on_inbound(msg) is msg
        assert plugin.on_command("test", "", {}) is None
        assert plugin.commands == []

    def test_meta_extraction(self):
        class MyPlugin(ChatPlugin):
            name = "my-plugin"
            version = "2.0.0"
            description = "My awesome plugin"
            author = "me"

        plugin = MyPlugin()
        meta = plugin.meta()
        assert meta.name == "my-plugin"
        assert meta.version == "2.0.0"


class TestPluginRegistry:
    def test_register_plugin(self):
        registry = PluginRegistry()

        class TestPlugin(ChatPlugin):
            name = "test-plugin"

        plugin = TestPlugin()
        assert registry.register(plugin)
        assert "test-plugin" in registry.all_plugins

    def test_duplicate_registration(self):
        registry = PluginRegistry()

        class TestPlugin(ChatPlugin):
            name = "test-plugin"

        assert registry.register(TestPlugin())
        assert not registry.register(TestPlugin())

    def test_activate_deactivate(self):
        registry = PluginRegistry()

        class TestPlugin(ChatPlugin):
            name = "test-plugin"

        registry.register(TestPlugin())
        assert registry.activate("test-plugin")
        assert registry.all_plugins["test-plugin"].state == PluginState.ACTIVE
        assert len(registry.active_plugins) == 1

        assert registry.deactivate("test-plugin")
        assert registry.all_plugins["test-plugin"].state == PluginState.LOADED
        assert len(registry.active_plugins) == 0

    def test_activate_nonexistent(self):
        registry = PluginRegistry()
        assert not registry.activate("nonexistent")

    def test_activate_all(self):
        registry = PluginRegistry()

        class P1(ChatPlugin):
            name = "p1"

        class P2(ChatPlugin):
            name = "p2"

        registry.register(P1())
        registry.register(P2())
        count = registry.activate_all()
        assert count == 2
        assert len(registry.active_plugins) == 2

    def test_process_outbound(self):
        registry = PluginRegistry()

        class UpperPlugin(ChatPlugin):
            name = "upper"

            def on_outbound(self, msg):
                return msg.model_copy(update={"content": msg.content.upper()})

        registry.register(UpperPlugin())
        registry.activate("upper")

        msg = _msg("hello")
        result = registry.process_outbound(msg)
        assert result.content == "HELLO"

    def test_process_inbound(self):
        registry = PluginRegistry()

        class TagPlugin(ChatPlugin):
            name = "tagger"

            def on_inbound(self, msg):
                metadata = dict(msg.metadata)
                metadata["tagged"] = True
                return msg.model_copy(update={"metadata": metadata})

        registry.register(TagPlugin())
        registry.activate("tagger")

        msg = _msg()
        result = registry.process_inbound(msg)
        assert result.metadata["tagged"] is True

    def test_handle_command(self):
        registry = PluginRegistry()

        class CmdPlugin(ChatPlugin):
            name = "cmd"

            @property
            def commands(self):
                return ["greet"]

            def on_command(self, command, args, context):
                if command == "greet":
                    return f"Hello, {args}!"
                return None

        registry.register(CmdPlugin())
        registry.activate("cmd")

        result = registry.handle_command("greet", "world", {})
        assert result == "Hello, world!"

        result = registry.handle_command("unknown", "", {})
        assert result is None

    def test_list_commands(self):
        registry = PluginRegistry()

        class CmdPlugin(ChatPlugin):
            name = "cmd"

            @property
            def commands(self):
                return ["a", "b"]

        registry.register(CmdPlugin())
        registry.activate("cmd")

        cmds = registry.list_commands()
        assert cmds == {"a": "cmd", "b": "cmd"}

    def test_error_in_activate(self):
        registry = PluginRegistry()

        class BrokenPlugin(ChatPlugin):
            name = "broken"

            def activate(self):
                raise RuntimeError("broke")

        registry.register(BrokenPlugin())
        assert not registry.activate("broken")
        assert registry.all_plugins["broken"].state == PluginState.ERROR
        assert "broke" in registry.all_plugins["broken"].error

    def test_error_in_hook_doesnt_crash(self):
        registry = PluginRegistry()

        class CrashPlugin(ChatPlugin):
            name = "crash"

            def on_outbound(self, msg):
                raise ValueError("boom")

        registry.register(CrashPlugin())
        registry.activate("crash")

        msg = _msg()
        result = registry.process_outbound(msg)
        assert result.content == "Hello world"

    def test_discover_builtins(self):
        registry = PluginRegistry()
        count = registry.discover()
        assert count >= 5
        assert "link-preview" in registry.all_plugins
        assert "code-format" in registry.all_plugins
        assert "ephemeral-helper" in registry.all_plugins
        assert "react-shortcut" in registry.all_plugins
        assert "status" in registry.all_plugins


class TestLinkPreviewPlugin:
    def test_detects_urls(self):
        plugin = LinkPreviewPlugin()
        msg = _msg("Check out https://example.com and http://foo.bar/baz")
        result = plugin.on_outbound(msg)
        assert "detected_urls" in result.metadata
        assert len(result.metadata["detected_urls"]) == 2

    def test_no_urls(self):
        plugin = LinkPreviewPlugin()
        msg = _msg("No links here")
        result = plugin.on_outbound(msg)
        assert result is msg

    def test_inbound_also_detects(self):
        plugin = LinkPreviewPlugin()
        msg = _msg("Visit https://skchat.io")
        result = plugin.on_inbound(msg)
        assert result.metadata["detected_urls"] == ["https://skchat.io"]


class TestCodeFormatPlugin:
    def test_detects_code_blocks(self):
        plugin = CodeFormatPlugin()
        msg = _msg("Here is code:\n```python\nprint('hello')\n```")
        result = plugin.on_inbound(msg)
        assert result.metadata["has_code"] is True
        assert "python" in result.metadata["code_languages"]

    def test_no_code(self):
        plugin = CodeFormatPlugin()
        msg = _msg("No code here")
        result = plugin.on_inbound(msg)
        assert result is msg


class TestEphemeralHelperPlugin:
    def test_burn_command(self):
        plugin = EphemeralHelperPlugin()
        result = plugin.on_command("burn", "60 Secret message", {})
        assert result == "__ephemeral__:60:Secret message"

    def test_burn_missing_args(self):
        plugin = EphemeralHelperPlugin()
        result = plugin.on_command("burn", "60", {})
        assert "Usage" in result

    def test_burn_invalid_ttl(self):
        plugin = EphemeralHelperPlugin()
        result = plugin.on_command("burn", "abc message", {})
        assert "Invalid TTL" in result

    def test_burn_ttl_out_of_range(self):
        plugin = EphemeralHelperPlugin()
        result = plugin.on_command("burn", "0 message", {})
        assert "between 1 and 86400" in result

    def test_commands_list(self):
        plugin = EphemeralHelperPlugin()
        assert "burn" in plugin.commands


class TestReactShortcutPlugin:
    def test_react_command(self):
        plugin = ReactShortcutPlugin()
        result = plugin.on_command(
            "react", "msg-123 thumbsup", {"sender": "capauth:alice@test"}
        )
        assert "__reaction__:msg-123:thumbsup:capauth:alice@test" in result

    def test_react_missing_args(self):
        plugin = ReactShortcutPlugin()
        result = plugin.on_command("react", "msg-123", {})
        assert "Usage" in result


class TestStatusPlugin:
    def test_whoami(self):
        plugin = StatusPlugin()
        result = plugin.on_command("whoami", "", {"sender": "capauth:alice@test"})
        assert "capauth:alice@test" in result

    def test_status_command(self):
        plugin = StatusPlugin()
        result = plugin.on_command("status", "", {"sender": "capauth:alice@test", "thread_id": "t1"})
        assert "SKChat Status" in result
        assert "capauth:alice@test" in result

    def test_commands_list(self):
        plugin = StatusPlugin()
        assert "status" in plugin.commands
        assert "whoami" in plugin.commands


class TestGetBuiltinPlugins:
    def test_returns_five_plugins(self):
        plugins = get_builtin_plugins()
        assert len(plugins) == 5
        names = {p.name for p in plugins}
        assert names == {"link-preview", "code-format", "ephemeral-helper", "react-shortcut", "status"}
