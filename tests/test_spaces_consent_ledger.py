from skchat.spaces.consent import ConsentLedger, can_record


def test_can_record_requires_all_speakers_consented(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    led.add("space-x", "alice@x.y")
    ok, missing = can_record(["alice@x.y", "bob@x.y"], "space-x", led)
    assert ok is False
    assert missing == ["bob@x.y"]


def test_can_record_ok_when_all_consented(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    led.add("space-x", "alice@x.y")
    led.add("space-x", "bob@x.y")
    ok, missing = can_record(["alice@x.y", "bob@x.y"], "space-x", led)
    assert ok is True
    assert missing == []


def test_consent_is_scoped_per_space(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    led.add("space-x", "alice@x.y")
    ok, missing = can_record(["alice@x.y"], "space-other", led)
    assert ok is False
    assert missing == ["alice@x.y"]      # consent in space-x doesn't carry over


def test_empty_speaker_list_can_record(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    ok, missing = can_record([], "space-x", led)
    assert ok is True                    # nobody on stage → nothing to consent to


def test_consent_persists(tmp_path):
    p = tmp_path / "consent.json"
    ConsentLedger(path=p).add("space-x", "alice@x.y")
    assert ConsentLedger(path=p).has("space-x", "alice@x.y") is True


def test_revoke_consent(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    led.add("space-x", "alice@x.y")
    led.revoke("space-x", "alice@x.y")
    assert led.has("space-x", "alice@x.y") is False
