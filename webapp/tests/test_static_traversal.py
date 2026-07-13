import pytest
from fastapi.testclient import TestClient

from webapp import auth
from webapp.main import app


@pytest.fixture
def authed_client():
    client = TestClient(app)
    cookie_value, _csrf = auth.create_session_cookie()
    client.cookies.set(auth.SESSION_COOKIE, cookie_value)
    return client


@pytest.mark.parametrize("path", [
    "/assets/../../.GarminDb/GarminConnectConfig.json",
    "/assets/%2e%2e%2f%2e%2e%2f.GarminDb/GarminConnectConfig.json",
    "/assets/..%2f..%2f.GarminDb/GarminConnectConfig.json",
    "/data/../../.GarminDb/GarminConnectConfig.json",
    "/data/../../HealthData/DBs/garmin_activities.db",
    "/assets/nonexistent/../../../.GarminDb/GarminConnectConfig.json",
])
def test_traversal_is_blocked(authed_client, path):
    resp = authed_client.get(path, follow_redirects=False)
    assert resp.status_code == 404


def test_forbidden_suffix_blocked_even_inside_docs(authed_client, tmp_path, monkeypatch):
    # Even a .db file placed *inside* docs/ (which should never happen, but
    # defense in depth) must not be servable.
    from webapp import config
    decoy = config.DOCS_DIR / "assets" / "decoy.db"
    decoy.write_text("not real data")
    try:
        resp = authed_client.get("/assets/decoy.db")
        assert resp.status_code == 404
    finally:
        decoy.unlink()


def test_unauthenticated_requests_are_rejected():
    client = TestClient(app)
    for path in ("/", "/assets/css/style.css", "/data/meta.json", "/api/race-config"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 401


def test_health_and_login_need_no_auth():
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/login").status_code == 200
