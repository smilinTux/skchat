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
