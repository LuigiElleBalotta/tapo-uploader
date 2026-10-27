"""A user can choose to keep the videos on the server (no folder to pick), when the administrator allows it."""
import pytest

from tapo_web.cameras import camera_data_dir
from tapo_web.config import Settings, SettingsError
from tapo_web.sources import label_for
from tapo_web.storage import Resolved, build, resolve
from tests.conftest import Env

DAY = "2026-10-26"


def test_settings_for_keeping_videos_default_to_unlimited():
    settings = Settings.from_env({})
    assert settings.local_keep_days == 0 and settings.local_quota_gb == 0 and settings.disk_min_free_gb == 1.0
    assert settings.local_root == "videos" or str(settings.local_root).endswith("videos")
    custom = Settings.from_env({"LOCAL_KEEP_DAYS": "30", "LOCAL_QUOTA_GB": "2.5", "DISK_MIN_FREE_GB": "0",
                                "STORAGE_LOCAL_ROOT": "/srv/tapo", "STORAGE_ALLOWED": "youtube,local"})
    assert (custom.local_keep_days, custom.local_quota_gb, custom.disk_min_free_gb) == (30, 2.5, 0)
    assert custom.local_root == "/srv/tapo" and custom.storage_allowed == ("youtube", "local")


@pytest.mark.parametrize("env", [{"LOCAL_KEEP_DAYS": "-1"}, {"LOCAL_KEEP_DAYS": "x"}, {"LOCAL_QUOTA_GB": "-3"},
                                 {"LOCAL_QUOTA_GB": "lots"}, {"DISK_MIN_FREE_GB": "-1"}])
def test_nonsense_numbers_are_refused(env):
    with pytest.raises(SettingsError):
        Settings.from_env(env)


def setup(tmp_path, **settings):
    values = dict(allow_own_storage=True, storage_allowed=("youtube", "webdav", "local"))
    values.update(settings)
    env = Env(tmp_path, **values)
    env.alice = env.add_local_user("alice", "correct horse")
    env.cid = env.db.add_camera(env.alice, "Garden", "cam.example.com", 38554, 38020, "stream1",
                                env.vault.encrypt("u"), env.vault.encrypt("p"))
    env.client = env.app.test_client()
    env.login("alice", "correct horse", client=env.client)
    return env


def post(env, **fields):
    return env.client.post("/storage", data={"csrf_token": env.csrf(env.client, "/storage"), **fields})


def test_page_offers_keeping_videos_on_the_server_only_when_allowed(tmp_path):
    env = setup(tmp_path)
    assert 'value="local"' in env.client.get("/storage").get_data(as_text=True)
    stricter = setup(tmp_path / "s", storage_allowed=("youtube", "webdav"))
    assert 'value="local"' not in stricter.client.get("/storage").get_data(as_text=True)


def test_choosing_it_saves_no_settings_and_no_path(tmp_path):
    env = setup(tmp_path)
    assert post(env, kind="local", folder="/etc").status_code == 302
    row = env.db.storage_config(env.alice)
    assert row["kind"] == "local" and row["config_enc"] is None
    resolved = resolve(env.settings, env.db, env.vault, env.alice)
    assert resolved.kind == "local" and resolved.source == "own" and resolved.config == {}


def test_not_allowed_by_the_administrator_is_refused(tmp_path):
    env = setup(tmp_path, storage_allowed=("youtube", "webdav"))
    assert post(env, kind="local").status_code == 400 and env.db.storage_config(env.alice) is None


def test_videos_land_in_the_servers_folder_split_by_user_and_camera(tmp_path):
    env = setup(tmp_path, storage_local_root=str(tmp_path / "vids"))
    post(env, kind="local")
    camera = env.db.camera_by_id(env.cid)
    ctx = type("Ctx", (), {"data_dir": camera_data_dir(env.settings, camera), "playlist_prefix": "Tapo"})()
    storage = build(resolve(env.settings, env.db, env.vault, env.alice), env.settings, ctx, env.app.extensions["tapo"]["resolver"])
    clip = tmp_path / "src.mp4"
    clip.write_bytes(b"x" * 100)
    storage.put(clip, DAY, "my clip", "")
    assert (tmp_path / "vids" / str(env.alice) / f"camera-{env.cid}" / f"Tapo {DAY}" / "my clip.mp4").read_bytes() == b"x" * 100


def test_without_a_configured_root_the_data_folder_is_used(tmp_path):
    env = setup(tmp_path)
    post(env, kind="local")
    camera = env.db.camera_by_id(env.cid)
    ctx = type("Ctx", (), {"data_dir": camera_data_dir(env.settings, camera), "playlist_prefix": "Tapo"})()
    storage = build(resolve(env.settings, env.db, env.vault, env.alice), env.settings, ctx, env.app.extensions["tapo"]["resolver"])
    assert str(storage.root).startswith(str(tmp_path)) and storage.root.parts[-2:] == (str(env.alice), f"camera-{env.cid}")


def test_the_choice_is_remembered_as_a_source_when_leaving_it(tmp_path):
    env = setup(tmp_path)
    post(env, kind="local")
    post(env, kind="youtube")
    assert [(r["kind"], r["origin"]) for r in env.db.storage_sources(env.alice)][-1] == ("local", "own")
    assert label_for(Resolved("local", {}, "own")) == "Server folder"
