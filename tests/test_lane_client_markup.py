from pathlib import Path

HTML = Path(
    "/home/cbrd21/clawd/skcapstone-repos/skchat/src/skchat/static/livekit.html"
).read_text()


def test_publishlane_mirrors_to_server_endpoint():
    assert "/lanes/event" in HTML
    assert "mirrorLaneToServer" in HTML


def test_catch_up_fetches_lane_state_on_join():
    assert "/lanes/" in HTML and "/state" in HTML
    assert "catchUpLane" in HTML
