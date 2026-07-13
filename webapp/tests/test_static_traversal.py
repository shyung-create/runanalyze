import pytest
from fastapi.testclient import TestClient

from webapp.main import app


@pytest.fixture
def client():
    # No login step anywhere anymore — a plain client can reach every GET
    # route directly, matching the tailnet-is-the-gate model.
    return TestClient(app)


@pytest.mark.parametrize("path", [
    "/assets/../../.GarminDb/GarminConnectConfig.json",
    "/assets/%2e%2e%2f%2e%2e%2f.GarminDb/GarminConnectConfig.json",
    "/assets/..%2f..%2f.GarminDb/GarminConnectConfig.json",
    "/data/../../.GarminDb/GarminConnectConfig.json",
    "/data/../../HealthData/DBs/garmin_activities.db",
    "/assets/nonexistent/../../../.GarminDb/GarminConnectConfig.json",
])
def test_traversal_is_blocked(client, path):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 404


def test_forbidden_suffix_blocked_even_inside_docs(client, tmp_path, monkeypatch):
    # Even a .db file placed *inside* docs/ (which should never happen, but
    # defense in depth) must not be servable.
    from webapp import config
    decoy = config.DOCS_DIR / "assets" / "decoy.db"
    decoy.write_text("not real data")
    try:
        resp = client.get("/assets/decoy.db")
        assert resp.status_code == 404
    finally:
        decoy.unlink()


def test_reads_need_no_cookie_at_all():
    client = TestClient(app)
    for path in ("/", "/assets/css/style.css", "/data/meta.json", "/api/race-config"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 200


def test_health_needs_no_cookie():
    client = TestClient(app)
    assert client.get("/health").status_code == 200
