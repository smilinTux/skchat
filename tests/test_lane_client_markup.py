from pathlib import Path

# Resolve relative to the repo, not a hardcoded absolute path (which broke CI,
# where the checkout lives under /home/runner/work, not a dev home dir).
HTML = (Path(__file__).resolve().parent.parent
        / "src" / "skchat" / "static" / "livekit.html").read_text()


def test_publishlane_mirrors_to_server_endpoint():
    assert "/lanes/event" in HTML
    assert "mirrorLaneToServer" in HTML


def test_catch_up_fetches_lane_state_on_join():
    assert "/lanes/" in HTML and "/state" in HTML
    assert "catchUpLane" in HTML
