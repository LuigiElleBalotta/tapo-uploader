"""Rotation of the videos kept on the server: by age, by the user's space limit, and when the disk runs low."""
from datetime import datetime

import pytest

from tapo_storage.local import LocalStorage
from tapo_web.cameras import camera_data_dir
from tapo_web.retention import effective_limits, run_local_retention
from tapo_web.sources import remember
from tapo_web.storage import Resolved, build
from tests.conftest import Env

GB = 1024 ** 3
NOW = datetime(2026, 10, 27, 12, 0, 0)


def storage(root, tmp_path, day, name, size):
    """Put a `size` bytes video in a LocalStorage rooted at `root`."""
    source = tmp_path / "src.mp4"
    source.write_bytes(b"x" * size)
    return LocalStorage(root, "Tapo").put(source, day, name, "00:00 x")


def setup(tmp_path, **settings):
    values = dict(storage_local_root=str(tmp_path / "vids"), disk_min_free_gb=0, allow_own_storage=True,
                  storage_allowed=("youtube", "webdav", "local"))
    values.update(settings)
    env = Env(tmp_path, **values)
    env.alice = env.add_local_user("alice", "correct horse")
    env.cid = env.db.add_camera(env.alice, "Garden", "cam.example.com", 38554, 38020, "stream1",
                                env.vault.encrypt("u"), env.vault.encrypt("p"))
    env.db.save_storage_config(env.alice, "local", None)
    env.root = tmp_path / "vids" / str(env.alice) / f"camera-{env.cid}"
    env.free = {"bytes": 100 * GB}
    return env


def run(env, **kw):
    return run_local_retention(env.settings, env.db, env.vault, env.app.extensions["tapo"]["resolver"], NOW,
                               free_bytes=lambda path: env.free["bytes"], **kw)


def days_left(env):
    return sorted(p.name for p in env.root.iterdir()) if env.root.exists() else []


def test_nothing_is_deleted_by_default(tmp_path):
    env = setup(tmp_path)
    storage(env.root, tmp_path, "2020-01-01", "ancient", 1000)
    state = run(env)
    assert days_left(env) == ["Tapo 2020-01-01"] and state["disk_low"] is False


def test_keep_days_deletes_whole_old_days_with_their_chapter_files(tmp_path):
    env = setup(tmp_path, local_keep_days=7)
    for day in ("2026-10-10", "2026-10-19", "2026-10-20", "2026-10-27"):
        storage(env.root, tmp_path, day, f"v {day}", 1000)
    run(env)
    assert days_left(env) == ["Tapo 2026-10-20", "Tapo 2026-10-27"]      # 7 days back from 27 October: the 20th stays
    assert not [p for p in env.root.rglob("*.txt") if "2026-10-1" in str(p)]


def test_a_person_can_choose_their_own_days_and_the_server_value_is_the_default(tmp_path):
    env = setup(tmp_path, local_keep_days=30)
    assert effective_limits(env.settings, env.db.user_settings(env.alice)) == (30, 0)
    env.db.set_user_setting(env.alice, "local_keep_days", 3)
    env.db.set_user_setting(env.alice, "local_quota_gb", 2.5)
    assert effective_limits(env.settings, env.db.user_settings(env.alice)) == (3, 2.5)
    env.db.set_user_setting(env.alice, "local_keep_days", 0)               # 0 is a real answer: keep forever
    assert effective_limits(env.settings, env.db.user_settings(env.alice))[0] == 0
    env.db.set_user_setting(env.alice, "local_keep_days", None)
    assert effective_limits(env.settings, env.db.user_settings(env.alice))[0] == 30


def test_the_space_limit_deletes_from_the_oldest_video_until_it_fits(tmp_path):
    env = setup(tmp_path, local_quota_gb=1)
    env.db.set_user_setting(env.alice, "local_quota_gb", 3000 / GB)         # a limit of 3000 bytes
    for index, day in enumerate(("2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27")):
        storage(env.root, tmp_path, day, f"v{index}", 1000)
    state = run(env)
    left = [p.name for p in env.root.rglob("*.mp4")]
    assert sorted(left) == ["v2.mp4", "v3.mp4"]            # 4 x 1007 bytes > 3000: the two oldest go, the newest stay
    user = state["users"][env.alice]
    assert user["trimmed"] >= 1 and user["full"] is True and user["used"] <= 3000


def test_unlimited_zero_means_no_limit_even_with_a_server_default_set_to_zero(tmp_path):
    env = setup(tmp_path, local_quota_gb=0)
    for day in ("2026-10-24", "2026-10-25"):
        storage(env.root, tmp_path, day, f"v {day}", 10_000_000)
    state = run(env)
    assert len(list(env.root.rglob("*.mp4"))) == 2 and state["users"][env.alice]["full"] is False


def test_low_disk_deletes_the_oldest_local_videos_of_anyone_until_there_is_room(tmp_path):
    env = setup(tmp_path, disk_min_free_gb=1)
    bob = env.add_local_user("bob", "another pass")
    bob_cam = env.db.add_camera(bob, "Door", "cam.example.com", 38554, 38020, "stream1", env.vault.encrypt("u"), env.vault.encrypt("p"))
    env.db.save_storage_config(bob, "local", None)
    bob_root = tmp_path / "vids" / str(bob) / f"camera-{bob_cam}"
    storage(env.root, tmp_path, "2026-10-25", "alice-old", 1000)
    storage(bob_root, tmp_path, "2026-10-24", "bob-oldest", 1000)
    storage(env.root, tmp_path, "2026-10-27", "alice-new", 1000)
    env.free["bytes"] = GB // 2
    original = env.free.copy()

    def free_bytes(path):
        remaining = len(list((tmp_path / "vids").rglob("*.mp4")))
        return original["bytes"] + (3 - remaining) * (GB // 4)       # every deleted video frees a quarter of a GB

    state = run_local_retention(env.settings, env.db, env.vault, env.app.extensions["tapo"]["resolver"], NOW, free_bytes=free_bytes)
    left = sorted(p.name for p in (tmp_path / "vids").rglob("*.mp4"))
    assert left == ["alice-new.mp4"] and state["disk_low"] is True       # oldest first, stopped as soon as there was room


def test_low_disk_with_nothing_local_to_delete_only_warns(tmp_path):
    env = setup(tmp_path, disk_min_free_gb=1)
    env.db.delete_storage_config(env.alice)
    env.free["bytes"] = GB // 10
    state = run(env)
    assert state["disk_low"] is True and state["free_gb"] < 0.2


def test_videos_in_the_cloud_and_waiting_clips_are_never_deleted(tmp_path):
    env = setup(tmp_path, local_keep_days=1, disk_min_free_gb=1, local_quota_gb=1 / GB)
    env.db.delete_storage_config(env.alice)                                # the person uses the default (YouTube) now
    pending = camera_data_dir(env.settings, env.db.camera_by_id(env.cid)) / "events" / "20200101"
    pending.mkdir(parents=True)
    (pending / "101500.mp4").write_bytes(b"x" * 5000)
    env.free["bytes"] = 1
    run(env)
    assert (pending / "101500.mp4").exists()


def test_earlier_local_destinations_are_rotated_too(tmp_path):
    env = setup(tmp_path, local_keep_days=7)
    env.db.delete_storage_config(env.alice)
    remember(env.db, env.vault, env.alice, Resolved("local", {}, "own"))     # it was "kept on the server" before
    storage(env.root, tmp_path, "2020-01-01", "ancient", 1000)
    run(env)
    assert days_left(env) == []


def test_a_deletion_that_fails_does_not_stop_the_rest(tmp_path, monkeypatch):
    env = setup(tmp_path, local_keep_days=1)
    storage(env.root, tmp_path, "2020-01-01", "a", 1000)
    storage(env.root, tmp_path, "2020-01-02", "b", 1000)
    real = LocalStorage.remove
    calls = []

    def flaky(self, day, name):
        calls.append(name)
        if name == "a.mp4":
            raise OSError("busy")
        return real(self, day, name)

    monkeypatch.setattr(LocalStorage, "remove", flaky)
    run(env)
    assert "a.mp4" in calls and "b.mp4" in calls and not (env.root / "Tapo 2020-01-02").exists()


def test_local_storage_reports_its_use_and_removes_a_video_with_its_chapters(tmp_path):
    root = tmp_path / "r"
    store = LocalStorage(root, "Tapo")
    storage(root, tmp_path, "2026-10-26", "v", 1000)
    assert store.usage() >= 1000 and [(i.day, i.name) for i in store.videos()] == [("2026-10-26", "v.mp4")]
    freed = store.remove("2026-10-26", "v.mp4")
    assert freed >= 1000 and not root.joinpath("Tapo 2026-10-26").exists() and store.usage() == 0
    with pytest.raises(Exception):
        store.remove("2026-10-26", "../x.mp4")
