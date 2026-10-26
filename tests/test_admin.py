from types import SimpleNamespace

import pytest

from tapo_web.quota import QuotaLedger
from tests.conftest import Env


class Supervisor:
    def __init__(self):
        self.syncs = 0

    def sync(self):
        self.syncs += 1

    def status(self, camera_id):
        return {"running": False, "pending_clips": 0}


def make(tmp_path, **settings):
    env = Env(tmp_path, **settings)
    env.admin = env.add_local_user("root", "admin password", role="admin")
    env.alice = env.add_local_user("alice", "correct horse")
    env.supervisor = Supervisor()
    env.app.extensions["tapo"]["supervisor"] = env.supervisor
    return env


def client_as(env, username, password):
    client = env.app.test_client()
    env.login(username, password, client=client)
    return client


def post(env, client, path, **data):
    return client.post(path, data={"csrf_token": env.csrf(client, "/"), **data})


def test_non_admin_gets_403_on_admin_routes(tmp_path):
    env = make(tmp_path)
    alice = client_as(env, "alice", "correct horse")
    assert alice.get("/admin").status_code == 403
    assert post(env, alice, f"/admin/users/{env.alice}/active", active="0").status_code == 403
    assert post(env, alice, f"/admin/users/{env.alice}/role", role="admin").status_code == 403
    assert env.db.user_by_id(env.alice)["role"] == "user"


def test_anonymous_cannot_see_admin(tmp_path):
    env = make(tmp_path)
    assert env.app.test_client().get("/admin").status_code == 302


def test_admin_page_lists_users_cameras_and_quota(tmp_path):
    env = make(tmp_path)
    cam = env.db.add_camera(env.alice, "Alice garden", "h", 38554, 38020, "stream1", env.vault.encrypt("u"), env.vault.encrypt("p"))
    env.db.set_camera_status(cam, status="online", last_error="something odd")
    QuotaLedger(env.db, env.settings.quota_daily).record(3402)
    text = client_as(env, "root", "admin password").get("/admin").get_data(as_text=True)
    for needed in ("root", "alice", "Alice garden", "online", "something odd", "3402 / 10000"):
        assert needed in text


def test_admin_can_deactivate_user_and_workers_stop(tmp_path):
    env = make(tmp_path)
    admin = client_as(env, "root", "admin password")
    response = post(env, admin, f"/admin/users/{env.alice}/active", active="0")
    assert response.status_code == 302
    assert env.db.user_by_id(env.alice)["active"] == 0 and env.supervisor.syncs == 1
    post(env, admin, f"/admin/users/{env.alice}/active", active="1")
    assert env.db.user_by_id(env.alice)["active"] == 1 and env.supervisor.syncs == 2


def test_admin_cannot_remove_last_admin(tmp_path):
    env = make(tmp_path)
    admin = client_as(env, "root", "admin password")
    assert post(env, admin, f"/admin/users/{env.admin}/active", active="0").status_code == 400
    assert post(env, admin, f"/admin/users/{env.admin}/role", role="user").status_code == 400
    user = env.db.user_by_id(env.admin)
    assert user["active"] == 1 and user["role"] == "admin"


def test_demoting_one_of_two_admins_is_fine(tmp_path):
    env = make(tmp_path)
    second = env.add_local_user("second", "second password", role="admin")
    admin = client_as(env, "root", "admin password")
    assert post(env, admin, f"/admin/users/{second}/role", role="user").status_code == 302
    assert env.db.user_by_id(second)["role"] == "user"


def test_role_change_for_local_users_only(tmp_path):
    env = make(tmp_path)
    admin = client_as(env, "root", "admin password")
    assert post(env, admin, f"/admin/users/{env.alice}/role", role="admin").status_code == 302
    assert env.db.user_by_id(env.alice)["role"] == "admin"
    nextcloud = env.db.add_user("nextcloud", "carol", "Carol", "user")
    response = post(env, admin, f"/admin/users/{nextcloud}/role", role="admin")
    assert response.status_code == 400 and env.db.user_by_id(nextcloud)["role"] == "user"


def test_unknown_user_and_bad_values_are_refused(tmp_path):
    env = make(tmp_path)
    admin = client_as(env, "root", "admin password")
    assert post(env, admin, "/admin/users/9999/active", active="0").status_code == 404
    assert post(env, admin, f"/admin/users/{env.alice}/role", role="superuser").status_code == 400
    assert post(env, admin, f"/admin/users/{env.alice}/active", active="maybe").status_code == 400


def test_settings_masked(tmp_path):
    env = make(tmp_path, nextcloud_url="https://cloud.example.com", nextcloud_client_id="nc-id",
               nextcloud_client_secret="nc-very-secret", google_client_json='{"web": {"client_secret": "g-very-secret"}}')
    text = client_as(env, "root", "admin password").get("/admin").get_data(as_text=True)
    for secret in (env.settings.encryption_key, env.settings.secret_key, "nc-very-secret", "g-very-secret"):
        assert secret not in text
    assert "203.0.113.7" in text and "10000" in text and "cloud.example.com" in text
