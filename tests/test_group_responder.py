from skchat.group_responder import GroupResponderConfig, load_group_config


def test_config_defaults_for_lumina():
    cfg = load_group_config("lumina", env={})
    assert cfg.agent == "lumina"
    assert cfg.backend_url == "http://localhost:18780/v1/chat/completions"
    assert cfg.model == "reg:ornith"
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


from skchat.group_responder import should_respond

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
