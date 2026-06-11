"""Cross-agent invariant: opus (start) and lumina (answer) land in the SAME room
with DISTINCT identities — driven through the real route handlers with stubbed I/O."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import skchat.call_routes as cr
from skchat.call_session import derive_room


def _make_client(monkeypatch, self_fqid, paired_fqid, sent):
    """A TestClient for one agent: it sees `paired_fqid` as its only peer."""
    monkeypatch.setattr(cr, "_have_creds", lambda: True)
    monkeypatch.setattr(cr, "_mint_token", lambda i, n, r, t: f"tok::{i}::{r}")
    monkeypatch.setattr(cr, "_self_fqid", lambda: self_fqid)
    monkeypatch.setattr(cr, "_list_peers", lambda: {paired_fqid: {"fingerprint": "x"}})
    monkeypatch.setattr(cr, "_send_invite", lambda **kw: sent.append(kw))
    app = FastAPI()
    cr.register_call_routes(app)
    return TestClient(app)


def test_opus_starts_lumina_answers_same_room(monkeypatch):
    sent: list = []
    # opus is the local agent here; it starts the call to lumina.
    opus = _make_client(monkeypatch, "opus@chef.skworld", "lumina@chef.skworld", sent)
    r_start = opus.post("/call/start", json={"peer": "lumina@chef.skworld"})
    assert r_start.status_code == 200
    start = r_start.json()

    # Now re-point the same module seams to lumina and have it answer opus.
    lumina = _make_client(monkeypatch, "lumina@chef.skworld", "opus@chef.skworld", sent)
    r_ans = lumina.post("/call/answer", json={"peer": "opus@chef.skworld"})
    assert r_ans.status_code == 200
    answer = r_ans.json()

    # Same room, distinct identities, exactly one CALL_INVITE (from start, not answer).
    assert start["room"] == answer["room"] == derive_room(
        "opus@chef.skworld", "lumina@chef.skworld"
    )
    assert start["identity"] != answer["identity"]
    assert len(sent) == 1
    assert sent[0]["to_fqid"] == "lumina@chef.skworld"
