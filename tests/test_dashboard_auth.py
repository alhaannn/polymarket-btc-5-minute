import time
import pytest
from polybot.dashboard.auth import AuthManager, hash_secret, verify_secret


def test_hash_and_verify_secret():
    secret = "MySuperSecretPassword123!"
    hashed = hash_secret(secret)
    assert hashed.startswith("pbkdf2_sha256$100000$")
    assert verify_secret(secret, hashed) is True
    assert verify_secret("WrongPassword", hashed) is False
    assert verify_secret("", hashed) is False


def test_plaintext_fallback_verification():
    plain = "simple123"
    assert verify_secret(plain, plain) is True
    assert verify_secret("other", plain) is False


def test_auth_manager_session_lifecycle():
    pwd_hash = hash_secret("admin_pass_99")
    pin_hash = hash_secret("7788")
    auth = AuthManager(
        username="alhan",
        password_hash=pwd_hash,
        pin_hash=pin_hash,
        session_expiry_sec=2,  # 2 seconds for test
        enabled=True,
    )

    # Failed login
    ok, err = auth.verify_login("alhan", "wrong")
    assert not ok
    assert "Invalid username" in err

    # Successful login
    ok, err = auth.verify_login("alhan", "admin_pass_99", ip="1.2.3.4")
    assert ok
    assert err == ""

    # Create & validate session
    token = auth.create_session("alhan")
    assert auth.validate_session(token) == "alhan"
    assert auth.validate_session("invalid-token") is None

    # Verify PIN
    assert auth.verify_pin("7788") is True
    assert auth.verify_pin("0000") is False

    # Logout
    auth.invalidate_session(token)
    assert auth.validate_session(token) is None


def test_cross_instance_hmac_session_validation():
    shared_secret = "super_secret_session_key_12345"
    instance_live = AuthManager(
        username="admin",
        password_hash=hash_secret("pass"),
        session_secret=shared_secret,
        enabled=True,
    )
    instance_paper = AuthManager(
        username="admin",
        password_hash=hash_secret("pass"),
        session_secret=shared_secret,
        enabled=True,
    )

    # Login and token generated on Live instance
    token = instance_live.create_session("admin")
    assert instance_live.validate_session(token) == "admin"

    # Paper instance (separate memory) validates token via cryptographic HMAC signature
    assert instance_paper.validate_session(token) == "admin"

    # Tampered token fails on both
    tampered = token[:-4] + "ffff"
    assert instance_live.validate_session(tampered) is None
    assert instance_paper.validate_session(tampered) is None


def test_brute_force_ip_lockout():
    auth = AuthManager(
        username="admin",
        password_hash=hash_secret("correct_pass"),
        max_failed_attempts=3,
        lockout_duration_sec=60,
        enabled=True,
    )
    ip = "192.168.1.100"

    # 3 failed attempts
    for _ in range(3):
        ok, _ = auth.verify_login("admin", "wrong_pass", ip=ip)
        assert not ok

    assert auth.is_ip_locked(ip) is True

    # 4th attempt should be blocked by lockout
    ok, err = auth.verify_login("admin", "correct_pass", ip=ip)
    assert not ok
    assert "locked" in err.lower()


def test_authenticated_dashboard_starlette_flow(tmp_path):
    from starlette.testclient import TestClient
    from polybot.dashboard.server import create_dashboard_app
    from polybot.dashboard.state import DashboardState
    from polybot.execution.ledger import TradeLedger

    ledger = TradeLedger(tmp_path / "test_auth_ledger.db")
    state = DashboardState()
    state.set_bot_status("RUNNING")

    auth = AuthManager(
        username="admin",
        password_hash=hash_secret("Alphora2026!SecureTrade"),
        pin_hash=hash_secret("7788"),
        enabled=True,
    )

    app = create_dashboard_app(state, ledger, auth_mgr=auth)
    client = TestClient(app, follow_redirects=False)

    # 1. Unauthenticated API request -> 401
    resp = client.get("/api/state")
    assert resp.status_code == 401
    assert resp.json()["status"] == "error"

    # 2. Unauthenticated UI request -> 303 Redirect to /login
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    # 3. Security headers check on responses
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"

    # 4. View login page -> 200
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Workstation Sign In" in resp.text

    # 5. Invalid credentials login -> 401
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "WrongPassword"})
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.json()["message"]

    # 6. Valid login -> 200 and session cookie
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "Alphora2026!SecureTrade"})
    assert resp.status_code == 200
    assert "session_id" in resp.cookies
    assert resp.json()["status"] == "ok"

    # 7. Access /api/auth/me with session
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    me = resp.json()
    assert me["authenticated"] is True
    assert me["pin_required"] is True
    assert me["username"] == "admin"

    # 8. Access protected /api/state with session
    resp = client.get("/api/state")
    assert resp.status_code == 200
    assert resp.json()["bot_status"] == "RUNNING"

    # 9. Control action without PIN -> 403 Forbidden
    resp = client.post("/api/control", json={"action": "pause"})
    assert resp.status_code == 403
    assert "PIN required" in resp.json()["message"]

    # 10. Control action with invalid PIN -> 403 Forbidden
    resp = client.post("/api/control", json={"action": "pause", "pin": "0000"})
    assert resp.status_code == 403
    assert "Invalid Security PIN" in resp.json()["message"]

    # 11. Control action with valid PIN 7788 -> 200 OK
    resp = client.post("/api/control", json={"action": "pause", "pin": "7788"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert state.pop_requested_action() == "PAUSE"

    # 12. Logout -> clears session
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200

    # 13. Subsquent protected API request -> 401 Unauthorized
    resp = client.get("/api/state")
    assert resp.status_code == 401

