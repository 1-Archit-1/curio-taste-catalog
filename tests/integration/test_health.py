async def test_healthz_reports_database_ok(client):
    # `client` overrides get_session and never runs the lifespan, so this passing also
    # shows the override works: without it there is no session_factory on app.state.
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
