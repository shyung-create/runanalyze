import pytest
from fastapi.testclient import TestClient

from webapp import auth, config
from webapp.main import app


@pytest.fixture(autouse=True)
def _insecure_cookies_for_test_transport(monkeypatch):
    # TestClient talks to http://testserver, not real TLS. A cookie set
    # with Secure=True (the correct production default, since this app
    # only ever runs behind Tailscale's HTTPS) is correctly never resent
    # by the client over that non-TLS transport between requests --
    # matching real browser behavior, not a bug. Tests that need a cookie
    # minted on one request to actually come back on the next need this.
    monkeypatch.setattr(config, "COOKIE_SECURE", False)


def test_get_routes_work_with_no_prior_cookie():
    """No login step exists — a fresh client with no cookie at all must
    still be able to load pages and call GET APIs. Tailscale reachability
    is the only gate."""
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/api/garmin/status").status_code == 200
    assert client.get("/api/race-config").status_code == 200


def test_first_visit_mints_a_csrf_cookie():
    client = TestClient(app)
    resp = client.get("/")
    assert auth.CSRF_COOKIE in resp.cookies
    set_cookie = resp.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


def test_csrf_token_endpoint_matches_the_cookie_that_was_set():
    client = TestClient(app)
    client.get("/")  # mints the cookie
    resp = client.get("/api/csrf-token")
    token = resp.json()["csrf_token"]
    assert token  # non-empty
    # Using that exact token as the header must satisfy require_csrf.
    resp2 = client.post(
        "/api/race-config/rest-days",
        json={"days": []},
        headers={"X-CSRF-Token": token},
    )
    assert resp2.status_code == 200


def test_response_never_contains_password_field():
    client = TestClient(app)
    resp = client.get("/api/garmin/status")
    assert resp.status_code == 200
    assert "password" not in resp.json()


def test_csrf_required_on_post_without_any_cookie():
    client = TestClient(app)
    resp = client.post("/api/refresh", json={})
    assert resp.status_code == 403


def test_csrf_required_on_blocked_dates_post_without_any_cookie():
    client = TestClient(app)
    resp = client.post("/api/race-config/blocked-dates", json={"dates": []})
    assert resp.status_code == 403


def test_csrf_required_on_rest_days_post_without_any_cookie():
    client = TestClient(app)
    resp = client.post("/api/race-config/rest-days", json={"days": ["monday"]})
    assert resp.status_code == 403


def test_csrf_rejects_mismatched_token():
    client = TestClient(app)
    client.get("/")  # mints a real cookie
    resp = client.post(
        "/api/race-config/rest-days",
        json={"days": []},
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert resp.status_code == 403


def test_csrf_rejects_forged_cookie_without_the_signing_key():
    client = TestClient(app)
    client.cookies.set(auth.CSRF_COOKIE, "garbage.not-signed-by-us")
    resp = client.post(
        "/api/race-config/rest-days",
        json={"days": []},
        headers={"X-CSRF-Token": "garbage"},
    )
    assert resp.status_code == 403
