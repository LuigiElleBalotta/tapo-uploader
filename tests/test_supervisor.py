import os
import socket
from datetime import datetime, timedelta

import pytest

from tapo_web.supervisor import Supervisor, discard_stale_clips, purge_inactive
from tapo_web.youtube_link import LinkRevoked
from tests.conftest import Env, public_resolver

NOW = datetime(2026, 11, 1, 12, 0, 0)


class FakeWorker:
    instances = []

    def __init__(self, ctx, on_status):
        self.ctx, self.on_status = ctx, on_status
        self.started = self.stopped = False
        self.alive_flag = True
        FakeWorker.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, timeout=10):
        self.stopped = True
        self.alive_flag = False

    @property
    def alive(self):
        return self.alive_flag and self.started and not self.stopped


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def reset_workers():
    FakeWorker.instances = []


def make(tmp_path, **kwargs):
    env = Env(tmp_path, **kwargs)
    env.clock = Clock()
    env.alice = env.add_local_user("alice", "correct horse")
    env.bob = env.add_local_user("bob", "another pass")
    env.sup = Supervisor(env.db, env.vault, env.settings, worker_factory=FakeWorker, clock=env.clock,
                         resolver=public_resolver)
    return env


def add_camera(env, user_id, name="cam", host="cam.example.com", enabled=True):
    cid = env.db.add_camera(user_id, name, host, 38554, 38020, "stream1", env.vault.encrypt("u"), env.vault.encrypt("p"))
    if enabled:
        env.db.set_camera_status(cid, enabled=1)
    return cid


def test_sync_starts_only_enabled_cameras_of_active_users(tmp_path):
    env = make(tmp_path)
    on = add_camera(env, env.alice, "on")
    add_camera(env, env.alice, "disabled", enabled=False)
    inactive = add_camera(env, env.bob, "bobs")
    env.db.set_user_active(env.bob, False)
    env.sup.sync()
    assert [w.ctx.name for w in FakeWorker.instances] == [f"camera-{on}"]
    assert FakeWorker.instances[0].started
    assert env.sup.status(inactive)["running"] is False


def test_sync_is_idempotent(tmp_path):
    env = make(tmp_path)
    add_camera(env, env.alice)
    env.sup.sync()
    env.sup.sync()
    assert len(FakeWorker.instances) == 1


def test_deactivated_user_worker_stopped_on_sync(tmp_path):
    env = make(tmp_path)
    cid = add_camera(env, env.alice)
    env.sup.sync()
    env.db.set_user_active(env.alice, False)
    env.sup.sync()
    assert FakeWorker.instances[0].stopped and env.sup.status(cid)["running"] is False


def test_unsafe_host_not_started_and_error_recorded(tmp_path):
    env = make(tmp_path)
    env.sup.resolver = lambda host, port=None, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
    cid = add_camera(env, env.alice)
    env.sup.sync()
    assert FakeWorker.instances == []
    camera = env.db.camera_for_user(env.alice, cid)
    assert camera["status"] == "blocked" and "not allowed" in camera["last_error"]


def test_status_events_update_camera_row(tmp_path):
    env = make(tmp_path)
    cid = add_camera(env, env.alice)
    env.sup.sync()
    report = FakeWorker.instances[0].on_status
    report("online", "")
    assert env.db.camera_for_user(env.alice, cid)["status"] == "online"
    env.clock.now += 5
    report("motion", "IsMotion")
    report("uploaded", "Tapo 2026-11-01 eventi 07:12-20:55 (5)")
    camera = env.db.camera_for_user(env.alice, cid)
    assert camera["last_motion"] == env.clock.now and camera["last_upload"] == env.clock.now
    assert camera["last_error"] is None
    report("offline", "ONVIF error: refused")
    camera = env.db.camera_for_user(env.alice, cid)
    assert camera["status"] == "offline" and "refused" in camera["last_error"]
    assert [e["kind"] for e in env.db.events_for_camera(cid)][:2] == ["offline", "uploaded"]


def test_same_error_is_recorded_once(tmp_path):
    env = make(tmp_path)
    cid = add_camera(env, env.alice)
    env.sup.sync()
    report = FakeWorker.instances[0].on_status
    for _ in range(5):
        report("error", "YouTube is not linked")
    assert [e["kind"] for e in env.db.events_for_camera(cid)].count("error") == 1


def test_changed_credentials_restart_the_worker(tmp_path):
    env = make(tmp_path)
    cid = add_camera(env, env.alice)
    env.sup.sync()
    env.db.set_camera_status(cid, password_enc=env.vault.encrypt("new-password"))
    env.sup.sync()
    assert FakeWorker.instances[0].stopped and len(FakeWorker.instances) == 2
    assert FakeWorker.instances[1].ctx.password == "new-password"


def test_worker_crash_restarts_with_backoff(tmp_path):
    env = make(tmp_path)
    add_camera(env, env.alice)
    env.sup.sync()
    FakeWorker.instances[0].alive_flag = False  # every thread died
    env.sup.sync()
    assert len(FakeWorker.instances) == 1  # backing off: not restarted yet
    env.clock.now += 400
    env.sup.sync()
    assert len(FakeWorker.instances) == 2 and FakeWorker.instances[1].started


def test_status_reports_pending_clips(tmp_path):
    env = make(tmp_path)
    cid = add_camera(env, env.alice)
    env.sup.sync()
    events = FakeWorker.instances[0].ctx.events_dir / "20261031"
    events.mkdir(parents=True)
    (events / "120000.mp4").write_bytes(b"x")
    (events / "130000.mp4").write_bytes(b"x")
    (events / "day.mp4").write_bytes(b"x")  # the joined file is not a pending clip
    status = env.sup.status(cid)
    assert status["running"] is True and status["pending_clips"] == 2


def test_stop_all_stops_workers(tmp_path):
    env = make(tmp_path)
    add_camera(env, env.alice)
    add_camera(env, env.bob, "bobs")
    env.sup.sync()
    env.sup.stop_all()
    assert all(w.stopped for w in FakeWorker.instances)


def test_youtube_factory_raises_link_revoked_when_not_linked(tmp_path):
    env = make(tmp_path)
    add_camera(env, env.alice)
    env.sup.sync()
    with pytest.raises(LinkRevoked):
        FakeWorker.instances[0].ctx.youtube()


# ----- retention -----

def test_purge_after_retention_removes_data_and_tokens(tmp_path):
    env = make(tmp_path)
    cid = add_camera(env, env.bob)
    env.db.save_youtube_link(env.bob, env.vault.encrypt("{}"), "chan", "shared")
    folder = tmp_path / "users" / str(env.bob)
    folder.mkdir(parents=True)
    (folder / "leftover.mp4").write_bytes(b"x")
    env.db.set_user_active(env.bob, False)
    deactivated = env.db.user_by_id(env.bob)["deactivated_at"]
    at = lambda days: datetime.fromtimestamp(deactivated) + timedelta(days=days)
    assert purge_inactive(env.db, tmp_path, at(29), 30) == 0
    assert env.db.user_by_id(env.bob) is not None and folder.exists()
    assert purge_inactive(env.db, tmp_path, at(31), 30) == 1
    assert env.db.user_by_id(env.bob) is None and not folder.exists()
    assert env.db.youtube_link(env.bob) is None and env.db.camera_for_user(env.bob, cid) is None
    assert env.db.user_by_id(env.alice) is not None  # active users are never purged


def test_active_users_are_never_purged(tmp_path):
    env = make(tmp_path)
    assert purge_inactive(env.db, tmp_path, NOW + timedelta(days=3650), 30) == 0


def test_stale_clips_discarded_after_seven_days_without_link(tmp_path):
    events = tmp_path / "events"
    old_day, new_day = events / "20261020", events / "20261030"
    old_day.mkdir(parents=True)
    new_day.mkdir(parents=True)
    old, new = old_day / "100000.mp4", new_day / "100000.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    then = (NOW - timedelta(days=8)).timestamp()
    os.utime(old, (then, then))
    recent = (NOW - timedelta(days=2)).timestamp()
    os.utime(new, (recent, recent))
    assert discard_stale_clips(events, NOW, 7) == 1
    assert not old.exists() and new.exists() and not old_day.exists()


def test_maintenance_discards_clips_only_for_unlinked_users(tmp_path):
    env = make(tmp_path)
    linked_cam, unlinked_cam = add_camera(env, env.alice, "a"), add_camera(env, env.bob, "b")
    env.db.save_youtube_link(env.alice, env.vault.encrypt("{}"), "chan", "shared")
    env.sup.sync()
    old = datetime.fromtimestamp(env.clock()) - timedelta(days=9)
    for worker in FakeWorker.instances:
        day = worker.ctx.events_dir / "20200101"
        day.mkdir(parents=True)
        clip = day / "100000.mp4"
        clip.write_bytes(b"x")
        os.utime(clip, (old.timestamp(), old.timestamp()))
    env.sup.maintenance(datetime.fromtimestamp(env.clock()))
    by_owner = {w.ctx.name: w.ctx.events_dir / "20200101" / "100000.mp4" for w in FakeWorker.instances}
    assert by_owner[f"camera-{linked_cam}"].exists()
    assert not by_owner[f"camera-{unlinked_cam}"].exists()
