from tests._route_walk import route_paths
def test_spaces_routes_are_registered_on_the_app():
    # Import the webui app and confirm a /spaces route exists.
    from skchat.webui import app

    paths = route_paths(app)
    assert "/spaces" in paths
    assert any(p == "/spaces/{space_id}/join" for p in paths)
