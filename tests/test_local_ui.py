"""Warnings on every page, the person's keep-days setting, the administrator's space limit, and the supervisor job."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from tests.conftest import Env
from tests.test_local_retention import GB, storage as put_video
from tests.test_supervisor import FakeWorker, make as make_supervisor

LOCAL = dict(allow_own_storage=True, storage_allowed=("youtube", "webdav", "local"))


def setup(tmp_path, **settings):
    values = dict(LOCAL, storage_local_root=str(tmp_path / "vids"), disk_min_free_gb=0)
    values.update(settings)
    env = Env(tmp_path, **values)
    env.alice = env.add_local_user("alice", "correct horse")
    env.admin = env.add_local_user("root", "admin pass", role="admin")
    env.cid = env.db.add_camera(env.alice, "Garden", "cam.example.com", 38554, 38020, "stream1",
                                env.vault.encrypt("u"), env.vault.encrypt("p"))
    env.client, env.admin_client = env.app.test_client(), env.app.test_client()
    env.login("alice", "correct horse", client=env.client)
    env.login("root", "admin pass", client=env.admin_client)
    return env


def with_state(env, **state):
    base = {"disk_low": False, "free_gb": 50.0, "users": {}}
    env.app.extensions["tapo"]["supervisor"] = SimpleNamespace(local_state={**base, **state}, status=lambda cid: {"running": True, "pending_clips": 0})


def post(env, path, client=None, **fields):
    client = client or env.client
    return client.post(path, data={"csrf_token": env.csrf(client, "/storage"), **fields})


# ----- warnings -----

def test_a_low_disk_warns_everybody_on_every_page(tmp_path):
    env = setup(tmp_path)
    with_state(env, disk_low=True, free_gb=0.4)
    for client in (env.client, env.admin_client):
        for path in ("/", "/storage"):
            text = client.get(path).get_data(as_text=True)
            assert "disk is almost full" in text and "0.4" in text and "never touched" in text


def test_no_warning_when_all_is_well(tmp_path):
    env = setup(tmp_path)
    with_state(env)
    assert "almost full" not in env.client.get("/").get_data(as_text=True)


def test_a_full_personal_space_warns_only_that_person(tmp_path):
    env = setup(tmp_path)
    with_state(env, users={env.alice: {"used": 2.9 * GB, "limit": 3 * GB, "full": True, "trimmed": 2, "keep_days": 0}})
    text = env.client.get("/").get_data(as_text=True)
    assert "Your space on the server is full" in text and "2.9 GB of 3.0 GB" in text
    assert "Your space on the server is full" not in env.admin_client.get("/").get_data(as_text=True)


def test_pages_work_without_a_supervisor(tmp_path):
    env = setup(tmp_path)
    assert env.client.get("/").status_code == 200 and env.client.get("/storage").status_code == 200


# ----- the person's keep days -----

def test_a_person_sets_their_own_days_and_can_go_back_to_the_servers(tmp_path):
    env = setup(tmp_path, local_keep_days=30)
    env.db.save_storage_config(env.alice, "local", None)
    page = env.client.get("/storage").get_data(as_text=True)
    assert "Videos kept on the server" in page and 'name="keep_days"' in page and "30 days" in page
    assert post(env, "/storage/local", keep_days="14").status_code == 302
    assert env.db.user_settings(env.alice)["local_keep_days"] == 14
    assert 'value="14"' in env.client.get("/storage").get_data(as_text=True)
    assert post(env, "/storage/local", keep_days="").status_code == 302
    assert env.db.user_settings(env.alice)["local_keep_days"] is None
    assert post(env, "/storage/local", keep_days="0").status_code == 302          # 0 = forever, a real answer
    assert env.db.user_settings(env.alice)["local_keep_days"] == 0


@pytest.mark.parametrize("value", ["-1", "abc", "1.5", "99999", " 1 2"])
def test_nonsense_days_are_refused(tmp_path, value):
    env = setup(tmp_path)
    env.db.save_storage_config(env.alice, "local", None)
    assert post(env, "/storage/local", keep_days=value).status_code == 400
    assert env.db.user_settings(env.alice)["local_keep_days"] is None


def test_the_card_is_only_there_when_keeping_on_the_server_is_possible(tmp_path):
    env = setup(tmp_path, storage_allowed=("youtube", "webdav"))
    assert "Videos kept on the server" not in env.client.get("/storage").get_data(as_text=True)
    assert post(env, "/storage/local", keep_days="3").status_code == 403


def test_the_card_shows_the_space_used_and_the_limit(tmp_path):
    env = setup(tmp_path, local_quota_gb=2)
    env.db.save_storage_config(env.alice, "local", None)
    with_state(env, users={env.alice: {"used": 0.5 * GB, "limit": 2 * GB, "full": False, "trimmed": 0, "keep_days": 0}})
    text = env.client.get("/storage").get_data(as_text=True)
    assert "0.5 GB of 2.0 GB" in text


# ----- the administrator's limit -----

def test_an_administrator_sets_and_clears_a_persons_space_limit(tmp_path):
    env = setup(tmp_path)
    assert post(env, f"/admin/users/{env.alice}/local-quota", client=env.admin_client, gb="5").status_code in (302, 200)
    assert env.db.user_settings(env.alice)["local_quota_gb"] == 5
    post(env, f"/admin/users/{env.alice}/local-quota", client=env.admin_client, gb="")
    assert env.db.user_settings(env.alice)["local_quota_gb"] is None
    post(env, f"/admin/users/{env.alice}/local-quota", client=env.admin_client, gb="0")
    assert env.db.user_settings(env.alice)["local_quota_gb"] == 0


@pytest.mark.parametrize("value", ["-1", "lots", "1e400", "nan"])
def test_nonsense_limits_are_refused(tmp_path, value):
    env = setup(tmp_path)
    assert post(env, f"/admin/users/{env.alice}/local-quota", client=env.admin_client, gb=value).status_code == 400
    assert env.db.user_settings(env.alice)["local_quota_gb"] is None


def test_a_normal_user_cannot_set_limits(tmp_path):
    env = setup(tmp_path)
    assert post(env, f"/admin/users/{env.alice}/local-quota", gb="100").status_code in (302, 403)
    assert env.db.user_settings(env.alice)["local_quota_gb"] is None


def test_the_admin_page_lists_the_limit_and_the_server_default(tmp_path):
    env = setup(tmp_path, local_quota_gb=4, local_keep_days=10)
    text = env.admin_client.get("/admin").get_data(as_text=True)
    assert 'name="gb"' in text and "10 days" in text


# ----- the background job -----

def test_the_supervisor_runs_the_rotation_and_keeps_the_state(tmp_path):
    env = make_supervisor(tmp_path, **LOCAL, storage_local_root=str(tmp_path / "vids"), local_keep_days=1, disk_min_free_gb=0)
    cid = env.db.add_camera(env.alice, "cam", "cam.example.com", 38554, 38020, "stream1", env.vault.encrypt("u"), env.vault.encrypt("p"))
    env.db.save_storage_config(env.alice, "local", None)
    root = tmp_path / "vids" / str(env.alice) / f"camera-{cid}"
    put_video(root, tmp_path, "2020-01-01", "old", 1000)
    put_video(root, tmp_path, "2026-11-01", "new", 1000)
    state = env.sup.local_rotation(datetime(2026, 11, 1, 12, 0, 0), free_bytes=lambda path: 100 * GB)
    assert [p.name for p in root.rglob("*.mp4")] == ["new.mp4"]
    assert env.sup.local_state is state and env.alice in state["users"]


def test_a_failing_rotation_never_stops_the_supervisor(tmp_path, monkeypatch):
    env = make_supervisor(tmp_path)
    import tapo_web.supervisor as module

    def boom(*a, **k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(module, "run_local_retention", boom)
    assert env.sup.local_rotation(datetime(2026, 11, 1, 12, 0, 0)) is env.sup.local_state
