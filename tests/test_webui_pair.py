from fastapi.testclient import TestClient

from skchat import webui


def _fake_bundle():
    from skcomms.pairing import PairingBundle

    return PairingBundle(
        fqid="lumina@chef.skworld",
        fingerprint="AB" * 20,
        syncthing_device_id="DEV-9",
        tailscale="lumina.ts.net",
        https="https://x/peers.json",
    )


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
    assert "sy=" not in j["uri"]  # syncthing dropped
    assert "ts=" in j["uri"]  # tailscale kept
    assert "https=" not in j["uri"]  # https dropped
    assert "fqid=" in j["uri"] and "fp=" in j["uri"]  # identity always present


def test_pair_qr_embed_key(monkeypatch):
    import skcomms.pairing as P

    def _bundle(agent=None, embed_key=False):
        b = _fake_bundle()
        if embed_key:
            b.pubkey = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nx\n-----END-----\n"
        return b

    monkeypatch.setattr(P, "bundle_from_self", _bundle)
    assert "pk=" in TestClient(webui.app).get("/pair/qr?embed=1").json()["uri"]
    assert "pk=" not in TestClient(webui.app).get("/pair/qr?embed=0").json()["uri"]


def test_pair_page_has_selector_and_qr():
    html = TestClient(webui.app).get("/pair").text
    for tok in ["/pair/qr", "Syncthing", "Tailscale", "HTTPS", "Embed", 'id="pair-qr"']:
        assert tok in html, tok


def test_pair_accept_ok(monkeypatch):
    import skcomms.pairing as P

    seen = {}

    def _accept(src, **kw):
        seen["src"] = src
        return {"fqid": "opus@chef.skworld", "fingerprint": "CD" * 20}

    monkeypatch.setattr(P, "accept_pairing", _accept)
    r = TestClient(webui.app).post(
        "/pair/accept", json={"uri": "skp://pair?v=1&fqid=opus@chef.skworld&fp=CDCD"}
    )
    assert r.status_code == 200, r.text
    assert seen["src"].startswith("skp://pair?")
    assert r.json()["fqid"] == "opus@chef.skworld"


def test_pair_accept_mismatch_is_400(monkeypatch):
    import skcomms.pairing as P

    def _accept(src, **kw):
        raise ValueError("fingerprint mismatch for opus@chef.skworld — refusing to pair")

    monkeypatch.setattr(P, "accept_pairing", _accept)
    r = TestClient(webui.app).post("/pair/accept", json={"uri": "skp://pair?v=1&fqid=x&fp=00"})
    assert r.status_code == 400
    assert "mismatch" in r.json()["detail"].lower()


def test_pair_accept_missing_uri_is_400():
    r = TestClient(webui.app).post("/pair/accept", json={})
    assert r.status_code == 400


def test_pair_scan_page_wiring():
    html = TestClient(webui.app).get("/pair/scan").text
    for tok in [
        "/pair/accept",
        "BarcodeDetector",
        "getUserMedia",
        "skp://",
        'id="manual"',
        "Pair",
    ]:
        assert tok in html, tok


def test_pair_qr_embed_too_big_falls_back_to_compact(monkeypatch):
    """A pubkey too large to embed in a QR falls back to a compact QR + warning."""
    import skcomms.pairing as P

    big = "X" * 6000  # an armored key way past QR capacity

    def _bundle(agent=None, embed_key=False):
        b = P.PairingBundle(
            fqid="opus@chef.skworld", fingerprint="CD" * 20, syncthing_device_id="DEV-2"
        )
        if embed_key:
            b.pubkey = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n" + big + "\n-----END-----\n"
        return b

    monkeypatch.setattr(P, "bundle_from_self", _bundle)
    j = TestClient(webui.app).get("/pair/qr?embed=1").json()
    assert j["embedded"] is False  # fell back
    assert "pk=" not in j["uri"]  # compact
    assert j["warning"] and "too large" in j["warning"].lower()
    assert "<svg" in j["svg"]  # still renders
