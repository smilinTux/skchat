"""Operator call observability — sk-alert with topic + one-press join link (e8651a65)."""

import skchat.call_observability as co


def test_join_url_contains_room_identity_token(monkeypatch):
    monkeypatch.setattr(co, "_mint_chef_token", lambda room: "TOK123")
    monkeypatch.setenv("SKCHAT_WEBUI_PUBLIC_URL", "https://host.ts.net")
    url = co.operator_join_url("call-xyz")
    assert url == "https://host.ts.net/livekit?room=call-xyz&identity=chef&token=TOK123"


def test_alert_operator_includes_topic_and_link(monkeypatch):
    sent = []
    monkeypatch.setattr(co, "_mint_chef_token", lambda room: "TOK")
    monkeypatch.setattr(co, "_sk_alert", lambda msg: sent.append(msg))
    co.alert_operator(
        from_fqid="opus@chef.skworld",
        to_fqid="lumina@chef.skworld",
        room="call-abc",
        topic="debugging the ingest pipeline",
    )
    assert len(sent) == 1
    msg = sent[0]
    assert "opus & lumina" in msg
    assert "debugging the ingest pipeline" in msg
    assert "room=call-abc" in msg
    assert "identity=chef" in msg


def test_alert_operator_without_topic(monkeypatch):
    sent = []
    monkeypatch.setattr(co, "_mint_chef_token", lambda room: "TOK")
    monkeypatch.setattr(co, "_sk_alert", lambda msg: sent.append(msg))
    co.alert_operator(from_fqid="opus@chef.skworld", to_fqid="lumina@chef.skworld", room="call-q")
    assert "opus & lumina" in sent[0]
    assert "topic:" not in sent[0]


def test_alert_operator_never_raises(monkeypatch):
    monkeypatch.setattr(
        co, "_mint_chef_token", lambda room: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    # must swallow the error
    co.alert_operator(from_fqid="a@x.y", to_fqid="b@x.y", room="r")
