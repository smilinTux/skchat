"""Tests for the SKChat plugin system."""

from unittest.mock import MagicMock, patch

from skchat.models import ChatMessage
from skchat.plugins import (
    ChatPlugin,
    PluginMeta,
    PluginRegistry,
    PluginState,
    SKChatPlugin,
)
from skchat.plugins_builtin import (
    CodeFormatPlugin,
    DaemonStatusPlugin,
    EchoPlugin,
    EphemeralHelperPlugin,
    LinkPreviewPlugin,
    ReactShortcutPlugin,
    StatusPlugin,
    TimePlugin,
    TranslatePlugin,
    WeatherPlugin,
    get_builtin_plugins,
    get_trigger_plugins,
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
        result = plugin.on_command("react", "msg-123 thumbsup", {"sender": "capauth:alice@test"})
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
        result = plugin.on_command(
            "status", "", {"sender": "capauth:alice@test", "thread_id": "t1"}
        )
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
        assert names == {
            "link-preview",
            "code-format",
            "ephemeral-helper",
            "react-shortcut",
            "status",
        }


# ---------------------------------------------------------------------------
# SKChatPlugin base class
# ---------------------------------------------------------------------------


class TestSKChatPluginBase:
    def test_can_subclass_and_implement(self):
        class PingPlugin(SKChatPlugin):
            name = "ping"
            triggers = ["ping"]

            def handle(self, message):
                return "pong"

        plugin = PingPlugin()
        msg = _msg("ping")
        assert plugin.should_handle(msg)
        assert plugin.handle(msg) == "pong"

    def test_default_should_handle_uses_triggers(self):
        class KwPlugin(SKChatPlugin):
            name = "kw"
            triggers = ["hello", "world"]

            def handle(self, message):
                return "matched"

        plugin = KwPlugin()
        assert plugin.should_handle(_msg("say hello there"))
        assert plugin.should_handle(_msg("world domination"))
        assert not plugin.should_handle(_msg("nothing relevant"))

    def test_should_handle_case_insensitive(self):
        class CiPlugin(SKChatPlugin):
            name = "ci"
            triggers = ["!PING"]

            def handle(self, message):
                return "pong"

        plugin = CiPlugin()
        assert plugin.should_handle(_msg("!ping"))
        assert plugin.should_handle(_msg("!PING"))
        assert plugin.should_handle(_msg("!Ping please"))


# ---------------------------------------------------------------------------
# PluginRegistry trigger methods
# ---------------------------------------------------------------------------


class TestPluginRegistryTriggers:
    def test_register_trigger_plugin(self):
        registry = PluginRegistry()

        class P(SKChatPlugin):
            name = "test-trigger"
            triggers = ["test"]

            def handle(self, msg):
                return "ok"

        assert registry.register_trigger(P())
        assert "test-trigger" in [p.name for p in registry.get_plugins()]

    def test_duplicate_trigger_registration_skipped(self):
        registry = PluginRegistry()

        class P(SKChatPlugin):
            name = "dup-trigger"
            triggers = ["dup"]

            def handle(self, msg):
                return "ok"

        assert registry.register_trigger(P())
        assert not registry.register_trigger(P())
        assert len([p for p in registry.get_plugins() if p.name == "dup-trigger"]) == 1

    def test_get_plugins_returns_all_registered(self):
        registry = PluginRegistry()

        class A(SKChatPlugin):
            name = "a-trigger"
            triggers = ["a"]

            def handle(self, msg):
                return "a"

        class B(SKChatPlugin):
            name = "b-trigger"
            triggers = ["b"]

            def handle(self, msg):
                return "b"

        registry.register_trigger(A())
        registry.register_trigger(B())
        names = {p.name for p in registry.get_plugins()}
        assert {"a-trigger", "b-trigger"}.issubset(names)

    def test_process_triggers_returns_replies(self):
        registry = PluginRegistry()

        class HelloPlugin(SKChatPlugin):
            name = "hello-trigger"
            triggers = ["hello"]

            def handle(self, msg):
                return "Hello back!"

        registry.register_trigger(HelloPlugin())
        replies = registry.process_triggers(_msg("hello there"))
        assert "Hello back!" in replies

    def test_process_triggers_no_match_returns_empty(self):
        registry = PluginRegistry()

        class NeverPlugin(SKChatPlugin):
            name = "never"
            triggers = ["!nevermatch99"]

            def handle(self, msg):
                return "should not appear"

        registry.register_trigger(NeverPlugin())
        assert registry.process_triggers(_msg("totally unrelated")) == []

    def test_process_triggers_exception_is_swallowed(self):
        registry = PluginRegistry()

        class BoomPlugin(SKChatPlugin):
            name = "boom-trigger"
            triggers = ["boom"]

            def handle(self, msg):
                raise RuntimeError("explode")

        registry.register_trigger(BoomPlugin())
        result = registry.process_triggers(_msg("boom"))
        assert result == []

    def test_discover_loads_trigger_plugins(self):
        registry = PluginRegistry()
        registry.discover()
        names = {p.name for p in registry.get_plugins()}
        assert {"echo", "daemon-status", "translate", "weather", "time"}.issubset(names)


# ---------------------------------------------------------------------------
# EchoPlugin
# ---------------------------------------------------------------------------


class TestEchoPlugin:
    def test_handles_echo_message(self):
        plugin = EchoPlugin()
        assert plugin.should_handle(_msg("echo: hello world"))

    def test_echo_case_insensitive(self):
        plugin = EchoPlugin()
        assert plugin.should_handle(_msg("Echo: hi"))
        assert plugin.should_handle(_msg("ECHO: test"))

    def test_ignores_non_echo(self):
        plugin = EchoPlugin()
        assert not plugin.should_handle(_msg("echoes of the past"))
        assert not plugin.should_handle(_msg("just a regular message"))

    def test_echo_returns_message(self):
        plugin = EchoPlugin()
        reply = plugin.handle(_msg("echo: Hello, world!"))
        assert reply == "Hello, world!"

    def test_echo_strips_whitespace(self):
        plugin = EchoPlugin()
        reply = plugin.handle(_msg("echo:   spaces around   "))
        assert reply == "spaces around"

    def test_echo_no_match_returns_none(self):
        plugin = EchoPlugin()
        assert plugin.handle(_msg("not an echo")) is None

    def test_echo_plugin_name_and_triggers(self):
        assert EchoPlugin.name == "echo"
        assert "echo:" in EchoPlugin.triggers


# ---------------------------------------------------------------------------
# DaemonStatusPlugin
# ---------------------------------------------------------------------------


class TestDaemonStatusPlugin:
    def test_handles_status_message(self):
        plugin = DaemonStatusPlugin()
        assert plugin.should_handle(_msg("!status"))
        assert plugin.should_handle(_msg("  !STATUS  "))

    def test_ignores_partial_status(self):
        plugin = DaemonStatusPlugin()
        assert not plugin.should_handle(_msg("!status extra args"))
        assert not plugin.should_handle(_msg("check !status"))

    def test_status_calls_daemon_status(self):
        plugin = DaemonStatusPlugin()
        fake_status = {
            "running": True,
            "uptime_seconds": 3665,
            "messages_received": 42,
            "messages_sent": 7,
            "transport_status": "ok",
            "online_peer_count": 3,
        }
        with patch("skchat.daemon.daemon_status", return_value=fake_status):
            reply = plugin.handle(_msg("!status"))
        assert reply is not None
        assert "Running: True" in reply
        assert "1h 1m 5s" in reply
        assert "42/7" in reply
        assert "Online peers: 3" in reply

    def test_status_handles_daemon_import_error(self):
        plugin = DaemonStatusPlugin()
        with patch("skchat.daemon.daemon_status", side_effect=RuntimeError("oops")):
            reply = plugin.handle(_msg("!status"))
        assert reply is not None
        assert "unavailable" in reply.lower()

    def test_status_plugin_name(self):
        assert DaemonStatusPlugin.name == "daemon-status"
        assert "!status" in DaemonStatusPlugin.triggers


# ---------------------------------------------------------------------------
# TranslatePlugin
# ---------------------------------------------------------------------------


class TestTranslatePlugin:
    def test_handles_translate_message(self):
        plugin = TranslatePlugin()
        assert plugin.should_handle(_msg("!translate fr: Hello"))
        assert plugin.should_handle(_msg("!translate es: Good morning"))

    def test_ignores_non_translate(self):
        plugin = TranslatePlugin()
        assert not plugin.should_handle(_msg("translate this text"))
        assert not plugin.should_handle(_msg("!weather London"))

    def test_translate_calls_subprocess(self):
        plugin = TranslatePlugin()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Bonjour"
        mock_result.stderr = ""
        with patch("skchat.plugins_builtin.subprocess.run", return_value=mock_result) as mock_run:
            reply = plugin.handle(_msg("!translate fr: Hello"))
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "trans" in call_args
        assert ":fr" in call_args
        assert reply == "Bonjour"

    def test_translate_missing_tool_returns_helpful_message(self):
        plugin = TranslatePlugin()
        with patch("skchat.plugins_builtin.subprocess.run", side_effect=FileNotFoundError):
            reply = plugin.handle(_msg("!translate de: Hello"))
        assert reply is not None
        assert "translate-shell" in reply or "trans" in reply

    def test_translate_timeout_returns_message(self):
        import subprocess as _sp

        plugin = TranslatePlugin()
        with patch(
            "skchat.plugins_builtin.subprocess.run",
            side_effect=_sp.TimeoutExpired("trans", 15),
        ):
            reply = plugin.handle(_msg("!translate fr: Hello"))
        assert reply is not None
        assert "timed out" in reply.lower()


# ---------------------------------------------------------------------------
# WeatherPlugin
# ---------------------------------------------------------------------------


class TestWeatherPlugin:
    def test_handles_weather_message(self):
        plugin = WeatherPlugin()
        assert plugin.should_handle(_msg("!weather London"))
        assert plugin.should_handle(_msg("!weather New York"))

    def test_ignores_non_weather(self):
        plugin = WeatherPlugin()
        assert not plugin.should_handle(_msg("what's the weather like?"))
        assert not plugin.should_handle(_msg("!translate fr: Hello"))

    def test_weather_calls_curl(self):
        plugin = WeatherPlugin()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "London: ⛅ +15°C"
        with patch("skchat.plugins_builtin.subprocess.run", return_value=mock_result) as mock_run:
            reply = plugin.handle(_msg("!weather London"))
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "curl" in call_args
        assert any("wttr.in" in a for a in call_args)
        assert reply == "London: ⛅ +15°C"

    def test_weather_city_with_spaces_encoded(self):
        plugin = WeatherPlugin()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "New York: ☀ +22°C"
        with patch("skchat.plugins_builtin.subprocess.run", return_value=mock_result) as mock_run:
            plugin.handle(_msg("!weather New York"))
        call_args = mock_run.call_args[0][0]
        url_arg = [a for a in call_args if "wttr.in" in a][0]
        assert "New+York" in url_arg

    def test_weather_missing_curl(self):
        plugin = WeatherPlugin()
        with patch("skchat.plugins_builtin.subprocess.run", side_effect=FileNotFoundError):
            reply = plugin.handle(_msg("!weather Berlin"))
        assert reply is not None
        assert "curl" in reply


# ---------------------------------------------------------------------------
# TimePlugin
# ---------------------------------------------------------------------------


class TestTimePlugin:
    def test_handles_time_message(self):
        plugin = TimePlugin()
        assert plugin.should_handle(_msg("!time"))
        assert plugin.should_handle(_msg("  !TIME  "))

    def test_ignores_partial_time(self):
        plugin = TimePlugin()
        assert not plugin.should_handle(_msg("!time zone"))
        assert not plugin.should_handle(_msg("what time is it"))

    def test_time_reply_contains_utc(self):
        plugin = TimePlugin()
        reply = plugin.handle(_msg("!time"))
        assert reply is not None
        assert "UTC" in reply

    def test_time_reply_contains_local(self):
        plugin = TimePlugin()
        reply = plugin.handle(_msg("!time"))
        assert "Local" in reply

    def test_time_plugin_name_and_triggers(self):
        assert TimePlugin.name == "time"
        assert "!time" in TimePlugin.triggers


# ---------------------------------------------------------------------------
# get_trigger_plugins()
# ---------------------------------------------------------------------------


class TestGetTriggerPlugins:
    def test_returns_five_plugins(self):
        plugins = get_trigger_plugins()
        assert len(plugins) == 5

    def test_all_are_skchats(self):
        for plugin in get_trigger_plugins():
            assert isinstance(plugin, SKChatPlugin)

    def test_expected_names(self):
        names = {p.name for p in get_trigger_plugins()}
        assert names == {"echo", "daemon-status", "translate", "weather", "time"}
