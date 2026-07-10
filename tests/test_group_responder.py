from skchat.group_responder import (
    GroupResponderConfig,
    load_group_config,
    GroupResponder,
)
from skchat.models import ChatMessage


def test_config_defaults_for_lumina():
    cfg = load_group_config("lumina", env={})
    assert cfg.agent == "lumina"
    assert cfg.backend_url == "http://localhost:18780/v1/chat/completions"
    assert cfg.model == "sk-default"
    # self-mentions include the agent name; @all/@both always match
    assert "@lumina" in cfg.mentions
    assert "@all" in cfg.mentions and "@both" in cfg.mentions
    assert cfg.on_error == "silent"


def test_config_env_overrides():
    cfg = load_group_config("opus", env={
        "SKCHAT_GROUP_BACKEND_URL": "http://localhost:8082/v1/chat/completions",
        "SKCHAT_GROUP_MODEL": "qwen3.6-27b-abliterated",
        "SKCHAT_GROUPS": "group:abc,group:def",
    })
    assert cfg.agent == "opus"
    assert "@opus" in cfg.mentions
    assert cfg.model == "qwen3.6-27b-abliterated"
    assert cfg.groups == ["group:abc", "group:def"]


from skchat.group_responder import should_respond, generate

_LUM = load_group_config("lumina", env={})


def test_should_respond_matrix():
    # addressed to me -> yes
    assert should_respond("@lumina hi", "chef@skworld.io", _LUM) is True
    # @all -> yes
    assert should_respond("@all standup?", "chef@skworld.io", _LUM) is True
    # addressed to the OTHER agent only -> no
    assert should_respond("@opus thoughts?", "chef@skworld.io", _LUM) is False
    # no mention -> no
    assert should_respond("just thinking out loud", "chef@skworld.io", _LUM) is False
    # my own message (loop guard) even if it contains @lumina -> no
    assert should_respond("@lumina echo", "capauth:lumina@skworld.io", _LUM) is False
    assert should_respond("@all echo", "lumina@chef.skworld.io", _LUM) is False


class _Resp:
    def __init__(self, code, data): self.status_code, self._d = code, data
    def json(self): return self._d


class _Http:
    def __init__(self, resp): self._resp, self.calls = resp, []
    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json)); return self._resp


def test_generate_ok():
    http = _Http(_Resp(200, {"choices": [{"message": {"content": "Hey Chef 🐧"}}]}))
    out = generate([{"role": "user", "content": "hi"}], _LUM, http=http)
    assert out == "Hey Chef 🐧"
    url, payload = http.calls[0]
    assert url == _LUM.backend_url
    assert payload["model"] == "sk-default"
    assert payload["messages"][0]["content"] == "hi"


def test_generate_http_error_returns_none():
    http = _Http(_Resp(500, {"error": "boom"}))
    assert generate([{"role": "user", "content": "hi"}], _LUM, http=http) is None


from skchat.group_responder import recall, store_turn


class _Mem:
    def __init__(self, hits=()): self._hits, self.snaps = list(hits), []
    def search(self, q, limit=5, **kw):
        return self._hits
    def snapshot(self, title, content, **kw): self.snaps.append((title, content, kw))


class _Hit:
    def __init__(self, c): self.content, self.title = c, "t"


def test_recall_formats_hits():
    mem = _Mem([_Hit("Chef likes teal"), _Hit("standup at 9")])
    out = recall("colors", store=mem)
    assert "Chef likes teal" in out and "standup at 9" in out


def test_recall_empty_on_error():
    class Boom:
        def search(self, *a, **k): raise RuntimeError("db down")
    assert recall("x", store=Boom()) == ""


def test_store_turn_snapshots():
    mem = _Mem()
    store_turn("q?", "a!", "group:xyz", store=mem)
    assert mem.snaps and mem.snaps[0][2]["source"] == "skchat"
    assert "group:xyz" in mem.snaps[0][2]["tags"]


class _Builder:
    def __init__(self):
        self.last_peer_name = None

    def build(self, peer_name=None):
        self.last_peer_name = peer_name
        return "You are Lumina. Warm, sovereign."


def _mk(content, sender="chef@skworld.io", recipient="group:room1"):
    return ChatMessage(sender=sender, recipient=recipient, content=content,
                       thread_id="room1")


def test_respond_when_mentioned():
    http = _Http(_Resp(200, {"choices": [{"message": {"content": "teal, Chef."}}]}))
    r = GroupResponder(_LUM, prompt_builder=_Builder(), http=http,
                       store=_Mem([_Hit("likes teal")]))
    out = r.respond(_mk("@lumina fav color?"))
    assert out == "teal, Chef."
    # system prompt + recall must be in the outbound messages
    _, payload = http.calls[0]
    roles = [m["role"] for m in payload["messages"]]
    assert roles[0] == "system"
    assert "Lumina" in payload["messages"][0]["content"]
    assert any("likes teal" in m["content"] for m in payload["messages"])


def test_respond_none_when_not_mentioned():
    http = _Http(_Resp(200, {"choices": [{"message": {"content": "x"}}]}))
    r = GroupResponder(_LUM, prompt_builder=_Builder(), http=http, store=_Mem())
    assert r.respond(_mk("@opus only you")) is None
    assert http.calls == []  # never hit the backend


def test_respond_uses_actual_sender_as_peer_name():
    """The soul-prompt peer should be the real (bare-handle) sender, not always 'chef'."""
    http = _Http(_Resp(200, {"choices": [{"message": {"content": "hi dave"}}]}))
    builder = _Builder()
    r = GroupResponder(_LUM, prompt_builder=builder, http=http, store=_Mem())
    r.respond(_mk("@lumina hi", sender="dave@skworld.io"))
    assert builder.last_peer_name == "dave"

    # a message from chef still addresses chef
    builder2 = _Builder()
    r2 = GroupResponder(_LUM, prompt_builder=builder2, http=http, store=_Mem())
    r2.respond(_mk("@lumina hi", sender="chef@skworld.io"))
    assert builder2.last_peer_name == "chef"


def test_should_respond_agent_sender_loop_guard():
    """Another agent's message never triggers a response (loop breaker)."""
    lum = load_group_config("lumina", env={})
    assert "opus" in lum.peer_agents  # default peer set excludes self
    # peer agent sender -> no response, even on a direct @self or @all mention
    assert should_respond("@lumina thoughts?", "capauth:opus@skworld.io", lum) is False
    assert should_respond("good idea @all", "capauth:opus@skworld.io", lum) is False
    assert should_respond("@all standup", "jarvis@chef.skworld.io", lum) is False
    # human (chef) sender -> normal mention rules still apply
    assert should_respond("@lumina hi", "chef@skworld.io", lum) is True
    assert should_respond("@all standup", "chef@skworld.io", lum) is True
    assert should_respond("just chatting", "chef@skworld.io", lum) is False
