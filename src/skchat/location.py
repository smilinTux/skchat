"""Location-pin typed message (Comms Suite Phase 4).

A location share is **just a typed message** on the P1 contract — no new
transport. It is:

    content_type = "location"
    body         = "📍 Shared location: <lat>,<lon>"   (Golden-rule fallback)
    rich         = {lat, lon, accuracy_m?, label?, precise: bool}

Security posture (operator-mandated, non-negotiable):
  * Opt-in only — the caller (the user tapping "Share location") is the sole
    trigger. Nothing here ever fetches or background-tracks a location.
  * **Coarse by default** — when ``precise`` is false the coordinates are
    rounded to ~2-3 decimal places (~1 km) so an approximate pin leaks no
    fine-grained position. The user must explicitly choose "precise".
  * One-shot pin only — no continuous live location in this phase.

This module owns the validation + coarse rounding + payload shaping so the
HTTP send path stays thin and the rules are unit-testable.
"""

from __future__ import annotations

from typing import Any, Optional

# Coarse rounding: ~3 decimal places ≈ 111 m at the equator (latitude); a touch
# under for longitude away from the equator. Field-standard "approximate" pin.
COARSE_DECIMALS = 3

# A precise share keeps full device precision but we still cap stored decimals
# so we never persist absurd float noise.
PRECISE_DECIMALS = 6


class LocationError(ValueError):
    """Raised when a location payload is missing or out of range."""


def _coerce_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LocationError(f"{name} must be a number") from exc


def coarsen(lat: float, lon: float) -> tuple[float, float]:
    """Round coordinates to the coarse (~1 km) grid — the approximate default."""
    return round(lat, COARSE_DECIMALS), round(lon, COARSE_DECIMALS)


def validate_coords(lat: Any, lon: Any) -> tuple[float, float]:
    """Validate + coerce a lat/lon pair, raising :class:`LocationError`.

    Latitude must be in [-90, 90]; longitude in [-180, 180].
    """
    flat = _coerce_float(lat, "lat")
    flon = _coerce_float(lon, "lon")
    if not (-90.0 <= flat <= 90.0):
        raise LocationError(f"lat out of range (-90..90): {flat}")
    if not (-180.0 <= flon <= 180.0):
        raise LocationError(f"lon out of range (-180..180): {flon}")
    return flat, flon


def build_location_payload(rich: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Normalise + validate a client ``rich`` payload for a location message.

    Enforces the security posture server-side regardless of what the client
    sent: coordinates are validated, and **when ``precise`` is not explicitly
    true the coordinates are coarsened** (approximate is the default — a client
    that omits the flag, or sends a bad value, gets the safe behaviour).

    Args:
        rich: The client-supplied ``rich`` dict (lat/lon required; accuracy_m,
            label, precise optional).

    Returns:
        A clean ``rich`` dict: ``{lat, lon, precise, accuracy_m?, label?}``.

    Raises:
        LocationError: If lat/lon are missing or out of range.
    """
    if not isinstance(rich, dict):
        raise LocationError("location message requires a rich payload {lat, lon}")
    if "lat" not in rich or "lon" not in rich:
        raise LocationError("location rich payload must include lat and lon")

    lat, lon = validate_coords(rich.get("lat"), rich.get("lon"))

    # Coarse by default: precise only when the client explicitly opted in.
    precise = rich.get("precise") is True
    if precise:
        lat, lon = round(lat, PRECISE_DECIMALS), round(lon, PRECISE_DECIMALS)
    else:
        lat, lon = coarsen(lat, lon)

    out: dict[str, Any] = {"lat": lat, "lon": lon, "precise": precise}

    # accuracy_m: optional, non-negative number. When coarse, surface the
    # coarse-grid uncertainty so clients can draw an honest radius (~1 km).
    if precise:
        acc = rich.get("accuracy_m")
        if acc is not None:
            try:
                facc = float(acc)
                if facc >= 0:
                    out["accuracy_m"] = facc
            except (TypeError, ValueError):
                pass
    else:
        out["accuracy_m"] = 1000.0

    label = rich.get("label")
    if isinstance(label, str) and label.strip():
        out["label"] = label.strip()[:120]

    return out


def location_body(payload: dict[str, Any]) -> str:
    """Build the human-readable ``body`` fallback (Golden rule).

    Dumb clients / non-location surfaces render this verbatim.
    """
    lat = payload.get("lat")
    lon = payload.get("lon")
    label = payload.get("label")
    approx = "" if payload.get("precise") else " (approx.)"
    base = f"📍 Shared location: {lat},{lon}{approx}"
    if label:
        return f"📍 {label}: {lat},{lon}{approx}"
    return base


def maps_url(payload: dict[str, Any]) -> str:
    """Return an OpenStreetMap deep-link for the pin (external-map open)."""
    lat = payload.get("lat")
    lon = payload.get("lon")
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=15/{lat}/{lon}"


def shape_location_message(
    rich: Optional[dict[str, Any]],
    body: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Validate a location share and return ``(body, rich)`` ready to persist.

    The server always derives the ``body`` from the validated (possibly
    coarsened) payload so the fallback text matches the stored coordinates —
    a client-supplied body is ignored for location messages to keep them
    consistent.
    """
    payload = build_location_payload(rich)
    return location_body(payload), payload
