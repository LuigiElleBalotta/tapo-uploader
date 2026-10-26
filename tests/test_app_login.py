import pytest

from tests.conftest import Env


def test_login_ok_and_logout(web):
    web.add_local_user("alice", "correct horse")
    response = web.login("alice", "correct horse")
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert "Alice" in web.client.get("/").get_data(as_text=True)
    out = web.client.post("/logout", data={"csrf_token": web.csrf(path="/")})
    assert out.status_code == 302 and "/login" in out.headers["Location"]
    assert web.client.get("/").status_code == 302


def test_wrong_password_rejected_and_rate_limited(web):
    web.add_local_user("alice", "correct horse")
    for _ in range(5):
        assert web.login("alice", "wrong").status_code == 401
    assert web.login("alice", "correct horse").status_code == 429


def test_rate_limit_is_per_username(web):
    web.add_local_user("alice", "correct horse")
    web.add_local_user("bob", "another pass")
    for _ in range(5):
        web.login("alice", "wrong")
    assert web.login("bob", "another pass").status_code == 302


def test_post_without_csrf_rejected(web):
    web.add_local_user("alice", "correct horse")
    response = web.client.post("/login", data={"username": "alice", "password": "correct horse"})
    assert response.status_code == 400


def test_disabled_user_cannot_log_in(web):
    uid = web.add_local_user("alice", "correct horse")
    web.db.set_user_active(uid, False)
    assert web.login("alice", "correct horse").status_code == 401


def test_session_of_deactivated_user_is_dropped(web):
    uid = web.add_local_user("alice", "correct horse")
    web.login("alice", "correct horse")
    web.db.set_user_active(uid, False)
    assert web.client.get("/").status_code == 302


def test_anonymous_redirected_to_login(web):
    response = web.client.get("/")
    assert response.status_code == 302 and response.headers["Location"].startswith("/login")


def test_open_redirect_blocked(web):
    web.add_local_user("alice", "correct horse")
    for target in ("https://evil.example.com", "//evil.example.com", "javascript:alert(1)"):
        response = web.login("alice", "correct horse", next=target)
        assert response.headers["Location"] == "/"
        web.client.post("/logout", data={"csrf_token": web.csrf(path="/")})


def test_cookie_flags(tmp_path):
    env = Env(tmp_path, public_url="https://tapo.example.com")
    cookie = env.client.get("/login", base_url="https://tapo.example.com").headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie


def test_local_login_can_be_disabled(tmp_path):
    env = Env(tmp_path, local_login=False)
    env.add_local_user("alice", "correct horse")
    with env.client.session_transaction() as session:  # the page has no form, so no token to read from it
        session["csrf"] = "token"
    response = env.client.post("/login", data={"username": "alice", "password": "x", "csrf_token": "token"})
    assert response.status_code == 404


def test_create_admin_creates_admin_once(web):
    from tapo_web.__main__ import create_admin
    from tapo_web.auth_local import verify_password
    uid = create_admin(web.db, "root", "long enough pw")
    user = web.db.user_by_id(uid)
    assert user["role"] == "admin" and user["origin"] == "local"
    assert verify_password("long enough pw", user["password_hash"])
    assert create_admin(web.db, "root", "other long pw") is None
    with pytest.raises(ValueError):
        create_admin(web.db, "weak", "short")
