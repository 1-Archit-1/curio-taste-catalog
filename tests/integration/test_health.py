async def test_healthz_reports_database_ok(client):
    # `client` overrides get_conn and never runs the lifespan, so this passing also
    # shows the override works: without it there is no pool on app.state.
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
