"""PairingGate — operator window + nonce + rate limit (Funnel hardening)."""
from skchat.pairing_gate import PairingGate


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_closed_by_default_rejects():
    g = PairingGate()
    ok, reason = g.check("anything")
    assert not ok and "not open" in reason


def test_open_window_allows_matching_nonce():
    clk = _Clock()
    g = PairingGate(now=clk)
    info = g.open_window()
    assert g.is_open()
    ok, _ = g.check(info["nonce"])
    assert ok


def test_wrong_nonce_rejected():
    g = PairingGate()
    g.open_window()
    ok, reason = g.check("not-the-nonce")
    assert not ok and "nonce" in reason


def test_window_expires():
    clk = _Clock()
    g = PairingGate(window_ttl=300, now=clk)
    info = g.open_window()
    clk.t += 301
    assert not g.is_open()
    ok, reason = g.check(info["nonce"])
    assert not ok and "not open" in reason


def test_accept_cap_auto_closes_window():
    clk = _Clock()
    g = PairingGate(max_accepts_per_window=2, now=clk)
    info = g.open_window()
    nonce = info["nonce"]
    assert g.check(nonce)[0]
    g.consume()
    assert g.check(nonce)[0]
    g.consume()  # hits cap → closes
    assert not g.is_open()
    assert not g.check(nonce)[0]


def test_rate_limit_throttles_attempts():
    clk = _Clock()
    g = PairingGate(max_attempts_per_throttle=5, throttle_window=60, now=clk)
    g.open_window()
    # 5 allowed, 6th+ throttled (all within the same throttle window)
    results = [g.check("x")[0] for _ in range(8)]  # wrong nonce, but attempts still counted
    # once throttled, reason flips to rate limited
    ok, reason = g.check("x")
    assert not ok and "rate limited" in reason


def test_rate_limit_window_slides():
    clk = _Clock()
    g = PairingGate(max_attempts_per_throttle=3, throttle_window=60, now=clk)
    g.open_window()
    for _ in range(5):
        g.check("x")
    assert "rate limited" in g.check("x")[1]
    clk.t += 61  # throttle window passes
    # not rate-limited anymore (though still wrong nonce)
    ok, reason = g.check("x")
    assert "rate limited" not in reason
