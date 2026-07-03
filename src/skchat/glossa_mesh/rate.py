"""SKGlossa rate adaptation (G2, spec §6 — "rate-adapt to the weaker model").

Tier negotiation (handshake) fixes a hard CEILING: the densest tier the weakest
peer can decode. Within that ceiling, link/peer conditions still vary at runtime,
so a static tier is either too fragile (dense tier over a lossy link) or wastes
density (robust tier over a clean link). ``RateController`` closes that loop:

  * poor conditions  -> DEGRADE fast (one tier down per bad observation) toward
    the robust L0 floor — graceful degradation is immediate, not patient.
  * good conditions  -> UPGRADE slowly (needs a sustained good streak) back up
    toward the ceiling — recovery is deliberate to avoid flapping.

The quality signal is pluggable: feed a [0,1] score to ``observe``, or use
``observe_network(loss, latency_ms)`` which maps loss/latency to a score. The
controller only ever proposes a tier; ``level(ceiling)`` clamps it into
[floor, ceiling] so it can never exceed what negotiation allows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Mirror the ladder without importing codec_ext (avoids any import cycle): the
# controller reasons over plain tier integers.
L0 = 0
L3 = 3


def quality_from_network(
    loss: float, latency_ms: float, *, latency_ceiling_ms: float = 400.0
) -> float:
    """Map an observed loss fraction [0,1] and one-way latency (ms) to a quality
    score in [0,1] (1 = pristine). Loss dominates (a lossy link corrupts dense
    frames); latency degrades linearly up to ``latency_ceiling_ms``."""
    loss = min(max(loss, 0.0), 1.0)
    lat_factor = 1.0 - min(max(latency_ms, 0.0) / latency_ceiling_ms, 1.0)
    return (1.0 - loss) * lat_factor


@dataclass
class RateController:
    """Adaptive tier selector with asymmetric hysteresis (fast down / slow up).

    Args:
        floor: lowest tier it will ever propose (the robust readable floor).
        max_tier: highest tier it will climb to (clamped again by ``level``'s
            ceiling — this just bounds the internal target).
        start: initial proposed tier (default: optimistic at ``max_tier``, so a
            clean link uses full density and only degrades under real trouble).
        up_quality: score strictly above which an observation counts as "good".
        down_quality: score strictly below which an observation forces a step down.
        up_patience: consecutive good observations required before a step up.
    """

    floor: int = L0
    max_tier: int = L3
    start: int | None = None
    up_quality: float = 0.7
    down_quality: float = 0.4
    up_patience: int = 3
    _target: int = field(init=False)
    _good_streak: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._target = self.start if self.start is not None else self.max_tier
        self._target = self._clamp(self._target)

    def _clamp(self, v: int) -> int:
        return max(self.floor, min(v, self.max_tier))

    @property
    def target(self) -> int:
        """The tier the controller currently proposes (before ceiling clamping)."""
        return self._target

    def observe(self, quality: float) -> int:
        """Feed a [0,1] quality score; adjust the proposed tier. Returns the new
        target. Below ``down_quality`` steps down immediately (graceful degrade);
        above ``up_quality`` builds a streak and steps up once patient enough."""
        if quality < self.down_quality:
            self._target = self._clamp(self._target - 1)
            self._good_streak = 0
        elif quality > self.up_quality:
            self._good_streak += 1
            if self._good_streak >= self.up_patience:
                self._target = self._clamp(self._target + 1)
                self._good_streak = 0
        else:
            # Neutral band: neither degrade nor build toward an upgrade.
            self._good_streak = 0
        return self._target

    def observe_network(self, loss: float, latency_ms: float) -> int:
        """Convenience: observe raw link stats via ``quality_from_network``."""
        return self.observe(quality_from_network(loss, latency_ms))

    def level(self, ceiling: int) -> int:
        """The tier to actually encode at: the proposed target clamped to the
        negotiated ``ceiling`` (never denser than the weakest peer can decode)."""
        return max(self.floor, min(self._target, ceiling))
