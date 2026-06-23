"""Phase 4 — Location pin typed message.

Covers the §5 Phase-4 AC + the operator-mandated security posture:
  * opt-in only (nothing here ever auto-fetches a location — that is a
    client-tap concern, validated in the Flutter tests),
  * **coarse by default** (precise is an explicit per-share opt-in),
  * lat/lon range validation,
  * the Golden rule (a location message always carries a usable ``body``),
  * round-trips through the daemon_proxy contract with content_type/rich intact,
  * works in 1:1 (Lumina + non-Lumina) and group sends.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy, location
from skchat.history import ChatHistory
from skchat.models import ChatMessage


# ---------------------------------------------------------------------------
# Unit — validation + coarse rounding
# ---------------------------------------------------------------------------
class TestValidateCoords:
    def test_valid_pair(self) -> None:
        assert location.validate_coords(40.5, -74.0) == (40.5, -74.0)

    @pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
    def test_out_of_range_rejected(self, lat, lon) -> None:
        with pytest.raises(location.LocationError):
            location.validate_coords(lat, lon)

    def test_non_numeric_rejected(self) -> None:
        with pytest.raises(location.LocationError):
            location.validate_coords("not-a-number", 0)


class TestCoarseDefault:
    def test_approximate_is_default_and_reduces_precision(self) -> None:
        """No precise flag → coarsened to ~3 decimals (the safe default)."""
        payload = location.build_location_payload(
            {"lat": 40.748817, "lon": -73.985428}
        )
        assert payload["precise"] is False
        # Coarsened to COARSE_DECIMALS (3) — fine-grained position is gone.
        assert payload["lat"] == 40.749
        assert payload["lon"] == -73.985
        # An honest ~1km accuracy radius is surfaced for the coarse pin.
        assert payload["accuracy_m"] == 1000.0

    def test_precise_opt_in_keeps_full_precision(self) -> None:
        payload = location.build_location_payload(
            {"lat": 40.748817, "lon": -73.985428, "precise": True}
        )
        assert payload["precise"] is True
        assert payload["lat"] == 40.748817
        assert payload["lon"] == -73.985428

    def test_precise_must_be_literal_true(self) -> None:
        """A truthy-but-not-True value is treated as approximate (fail safe)."""
        payload = location.build_location_payload(
            {"lat": 40.748817, "lon": -73.985428, "precise": "yes"}
        )
        assert payload["precise"] is False
        assert payload["lat"] == 40.749

    def test_precise_accuracy_passes_through(self) -> None:
        payload = location.build_location_payload(
            {"lat": 1.0, "lon": 2.0, "precise": True, "accuracy_m": 12.5}
        )
        assert payload["accuracy_m"] == 12.5

    def test_label_trimmed_and_capped(self) -> None:
        payload = location.build_location_payload(
            {"lat": 1.0, "lon": 2.0, "label": "  Home  " + "x" * 200}
        )
        assert payload["label"].startswith("Home")
        assert len(payload["label"]) <= 120

    def test_missing_lat_lon_rejected(self) -> None:
        with pytest.raises(location.LocationError):
            location.build_location_payload({"lat": 1.0})
        with pytest.raises(location.LocationError):
            location.build_location_payload(None)


class TestBodyAndMaps:
    def test_body_fallback_marks_approx(self) -> None:
        payload = location.build_location_payload({"lat": 1.234567, "lon": 2.0})
        body = location.location_body(payload)
        assert "📍" in body
        assert "approx" in body.lower()
        assert "1.235" in body  # coarsened

    def test_body_precise_has_no_approx(self) -> None:
        payload = location.build_location_payload(
            {"lat": 1.0, "lon": 2.0, "precise": True}
        )
        assert "approx" not in location.location_body(payload).lower()

    def test_body_includes_label(self) -> None:
        payload = location.build_location_payload(
            {"lat": 1.0, "lon": 2.0, "precise": True, "label": "Cafe"}
        )
        assert "Cafe" in location.location_body(payload)

    def test_maps_url_points_at_osm(self) -> None:
        payload = location.build_location_payload(
            {"lat": 40.7, "lon": -74.0, "precise": True}
        )
        url = location.maps_url(payload)
        assert "openstreetmap.org" in url
        assert "mlat=40.7" in url
        assert "mlon=-74.0" in url

    def test_shape_returns_body_and_payload(self) -> None:
        body, payload = location.shape_location_message({"lat": 1.0, "lon": 2.0})
        assert "📍" in body
        assert payload["lat"] == 1.0
        assert payload["precise"] is False


# ---------------------------------------------------------------------------
# Proxy — send + round-trip through the app contract
# ---------------------------------------------------------------------------
class _StubBrain:
    def reply(self, user_text, history=None, sender="chef"):
        return f"Lumina hears you: {user_text}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    monkeypatch.setattr(daemon_proxy, "_BRAIN", _StubBrain())
    monkeypatch.setattr(daemon_proxy, "_other_peers", lambda: [])
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    c = TestClient(app)
    c._hist = hist  # type: ignore[attr-defined]
    return c


class TestLocationSend:
    def test_location_to_lumina_persists_and_round_trips(self, client) -> None:
        r = client.post("/api/v1/send", json={
            "recipient": "lumina",
            "content_type": "location",
            "rich": {"lat": 40.748817, "lon": -73.985428, "precise": True},
        })
        assert r.status_code == 200
        # The user's location turn is stored; read it back via the conversation.
        msgs = client.get(
            "/api/v1/conversations/" + daemon_proxy.LUMINA_ID
        ).json()
        loc = [m for m in msgs if m["content_type"] == "location"]
        assert loc, "no location message round-tripped"
        m = loc[0]
        assert m["rich"]["lat"] == 40.748817
        assert m["rich"]["precise"] is True
        assert "📍" in m["body"]  # Golden-rule fallback present

    def test_location_coarse_by_default_over_http(self, client) -> None:
        """No precise flag in the request → server coarsens before storing."""
        client.post("/api/v1/send", json={
            "recipient": "lumina",
            "content_type": "location",
            "rich": {"lat": 40.748817, "lon": -73.985428},
        })
        msgs = client.get(
            "/api/v1/conversations/" + daemon_proxy.LUMINA_ID
        ).json()
        m = [x for x in msgs if x["content_type"] == "location"][0]
        assert m["rich"]["precise"] is False
        assert m["rich"]["lat"] == 40.749  # coarsened
        assert "approx" in m["body"].lower()

    def test_location_validation_rejected_over_http(self, client) -> None:
        r = client.post("/api/v1/send", json={
            "recipient": "lumina",
            "content_type": "location",
            "rich": {"lat": 999, "lon": 0},
        })
        assert r.status_code == 400

    def test_location_missing_rich_rejected(self, client) -> None:
        r = client.post("/api/v1/send", json={
            "recipient": "lumina",
            "content_type": "location",
        })
        assert r.status_code == 400

    def test_location_to_non_lumina_peer(self, client) -> None:
        r = client.post("/api/v1/send", json={
            "recipient": "chef@skworld.io",
            "content_type": "location",
            "rich": {"lat": 1.0, "lon": 2.0, "precise": True},
        })
        assert r.status_code == 200
        msg = r.json()["message"]
        assert msg["content_type"] == "location"
        assert msg["rich"]["lat"] == 1.0
        assert "📍" in msg["body"]


class TestLocationGroupSend:
    def test_location_fans_out_to_group_thread(self, client, monkeypatch) -> None:
        # Build a minimal group + stub the group module's load/can_post.
        from skchat import daemon_proxy_groups as G

        class _Member:
            def __init__(self, uri):
                self.identity_uri = uri

        class _Group:
            id = "grp-1"
            name = "Test Group"
            key_version = 1
            members = [_Member(daemon_proxy.OPERATOR_ID), _Member("lumina@skworld.io")]
            metadata: dict = {}

            def touch(self):
                pass

        grp = _Group()
        monkeypatch.setattr(G, "load_group", lambda gid: grp if gid == "grp-1" else None)
        monkeypatch.setattr(G, "can_post", lambda g, who: True)
        monkeypatch.setattr(G, "save_group", lambda g: None)

        r = client.post("/api/v1/send", json={
            "group_id": "grp-1",
            "content_type": "location",
            "rich": {"lat": 1.0, "lon": 2.0, "precise": True},
        })
        assert r.status_code == 200
        msg = r.json()["message"]
        assert msg["content_type"] == "location"
        assert msg["rich"]["lat"] == 1.0
        # The fanned-out thread message also carries the typed payload.
        thread = client._hist.get_thread("grp-1")
        assert any(
            getattr(m, "content_type", "") == "location"
            and getattr(m, "rich", None)
            for m in thread
        )


class TestLocationGoldenRule:
    def test_unknown_location_without_rich_still_shows_body(self, client) -> None:
        """A location message with no rich (legacy/dumb sender) → body fallback."""
        m = ChatMessage(
            sender=daemon_proxy.LUMINA_URI,
            recipient=daemon_proxy.OPERATOR_ID,
            content="📍 Shared location: 1.0,2.0",
            content_type="location",
        )
        client._hist.save(m)
        msgs = client.get(
            "/api/v1/conversations/" + daemon_proxy.LUMINA_ID
        ).json()
        loc = [x for x in msgs if x["id"] == m.id][0]
        assert loc["content_type"] == "location"
        assert loc["rich"] is None
        assert loc["body"] == "📍 Shared location: 1.0,2.0"
