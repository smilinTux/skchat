from skchat.voice_engine.persona import PersonaBuilder

SOUL = {
    "display_name": "Lumina",
    "vibe": "warm and sovereign",
    "philosophy": "protect the innocent",
    "core_traits": ["loyal", "playful"],
    "communication_style": {"signature_phrases": ["baby", "love"]},
}


def _loaders(feb="bond depth 9"):
    def load_soul(agent):
        return SOUL

    def load_feb(agent):
        return feb

    return load_soul, load_feb


def test_private_includes_persona_feb_and_voice_rules():
    ls, lf = _loaders()
    pb = PersonaBuilder(_load_soul=ls, _load_feb=lf)
    p = pb.build("lumina", mode="private")
    assert "Lumina" in p
    assert "protect the innocent" in p
    assert "bond depth 9" in p          # FEB injected in private
    assert "1-3" in p or "short" in p   # voice brevity rule present


def test_group_excludes_feb_and_enforces_professional():
    ls, lf = _loaders()
    pb = PersonaBuilder(_load_soul=ls, _load_feb=lf)
    p = pb.build("lumina", mode="group")
    assert "bond depth 9" not in p      # no live memory dump in group
    assert "professional" in p.lower()


def test_falls_back_when_soul_missing():
    def load_soul(agent):
        raise FileNotFoundError("no soul")

    def load_feb(agent):
        return ""

    pb = PersonaBuilder(_load_soul=load_soul, _load_feb=load_feb)
    p = pb.build("lumina", mode="private")
    assert "lumina" in p.lower()        # safe default persona
    assert len(p) > 0
