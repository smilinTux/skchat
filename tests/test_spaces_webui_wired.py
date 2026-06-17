def test_spaces_routes_are_registered_on_the_app():
    # Import the webui app and confirm a /spaces route exists.
    from skchat.webui import app

    paths = {r.path for r in app.routes}
    assert "/spaces" in paths
    assert any(p == "/spaces/{space_id}/join" for p in paths)
