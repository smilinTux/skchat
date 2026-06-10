from fastapi.testclient import TestClient
from skchat import webui


def _fake_bundle():
    from skcomms.pairing import PairingBundle
    return PairingBundle(fqid="lumina@chef.skworld", fingerprint="AB"*20,
                         syncthing_device_id="DEV-9", tailscale="lumina.ts.net",
                         https="https://x/peers.json")


def test_pair_qr_default_includes_all_hints(monkeypatch):
    import skcomms.pairing as P
    monkeypatch.setattr(P, "bundle_from_self", lambda agent=None, embed_key=False: _fake_bundle())
    r = TestClient(webui.app).get("/pair/qr")
    assert r.status_code == 200
    j = r.json()
    assert j["uri"].startswith("skp://pair?")
    assert "sy=DEV-9" in j["uri"] and "ts=" in j["uri"] and "https=" in j["uri"]
    assert "<svg" in j["svg"]


def test_pair_qr_drops_deselected_hints(monkeypatch):
    import skcomms.pairing as P
    monkeypatch.setattr(P, "bundle_from_self", lambda agent=None, embed_key=False: _fake_bundle())
    r = TestClient(webui.app).get("/pair/qr?sy=0&ts=1&https=0")
    j = r.json()
    assert "sy=" not in j["uri"]      # syncthing dropped
    assert "ts=" in j["uri"]          # tailscale kept
    assert "https=" not in j["uri"]   # https dropped
    assert "fqid=" in j["uri"] and "fp=" in j["uri"]   # identity always present


def test_pair_qr_embed_key(monkeypatch):
    import skcomms.pairing as P
    def _bundle(agent=None, embed_key=False):
        b = _fake_bundle()
        if embed_key: b.pubkey = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nx\n-----END-----\n"
        return b
    monkeypatch.setattr(P, "bundle_from_self", _bundle)
    assert "pk=" in TestClient(webui.app).get("/pair/qr?embed=1").json()["uri"]
    assert "pk=" not in TestClient(webui.app).get("/pair/qr?embed=0").json()["uri"]
