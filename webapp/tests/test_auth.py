import time

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from webapp import auth, config
from webapp.main import app


def test_verify_dashboard_password(monkeypatch):
    real_hash = PasswordHasher().hash("correct-horse-battery-staple")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD_HASH", real_hash)
    assert auth.verify_dashboard_password("correct-horse-battery-staple") is True
    assert auth.verify_dashboard_password("wrong") is False


def test_verify_with_no_hash_configured_never_authenticates(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD_HASH", "")
    assert auth.verify_dashboard_password("anything") is False


def test_login_rate_limit_locks_out(monkeypatch):
    real_hash = PasswordHasher().hash("correct-horse-battery-staple")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD_HASH", real_hash)
    auth._attempts.clear()
    auth._locked_until.clear()

    client = TestClient(app)
    for _ in range(config.LOGIN_MAX_ATTEMPTS):
        resp = client.post("/login", data={"password": "wrong"})
        assert resp.status_code == 401

    # One more attempt, even with the CORRECT password, should now be locked out.
    resp = client.post("/login", data={"password": "correct-horse-battery-staple"})
    assert resp.status_code == 429


def test_login_success_sets_secure_httponly_samesite_cookie(monkeypatch):
    real_hash = PasswordHasher().hash("correct-horse-battery-staple")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD_HASH", real_hash)
    auth._attempts.clear()
    auth._locked_until.clear()

    client = TestClient(app)
    resp = client.post("/login", data={"password": "correct-horse-battery-staple"}, follow_redirects=False)
    assert resp.status_code == 303
    set_cookie = resp.headers.get("set-cookie", "")
    assert auth.SESSION_COOKIE in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


def test_response_never_contains_password_field():
    cookie_value, csrf = auth.create_session_cookie()
    client = TestClient(app)
    client.cookies.set(auth.SESSION_COOKIE, cookie_value)
    resp = client.get("/api/garmin/status")
    assert resp.status_code == 200
    assert "password" not in resp.json()


def test_csrf_required_on_post():
    cookie_value, _csrf = auth.create_session_cookie()
    client = TestClient(app)
    client.cookies.set(auth.SESSION_COOKIE, cookie_value)
    resp = client.post("/api/refresh", json={})
    assert resp.status_code == 403
