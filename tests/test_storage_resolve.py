"""Which destination a user's camera uses, how it is built, and the supervisor wiring."""
import json
import os
import time
from datetime import datetime, time as dtime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tapo_storage.base import StorageError
from tapo_storage.local import LocalStorage
from tapo_storage.webdav import WebDAVStorage
from tapo_web.storage import build, resolve
from tests.conftest import Env, make_settings, public_resolver
from tests.test_supervisor import FakeWorker, add_camera, make

OWN = {"url": "https://cloud.friend.example/remote.php/dav/files/me", "user": "me", "password": "S3cretPW", "folder": "Tapo"}


def save_own(env, user_id, kind="webdav", config=OWN):
    env.db.save_storage_config(user_id, kind, env.vault.encrypt(json.dumps(config)) if config is not None else None)


def user(tmp_path, **settings):
    env = Env(tmp_path, **settings)
    return env, env.add_local_user("alice", "correct horse")


def test_resolve_defaults_to_server_kind(tmp_path):
    env, uid = user(tmp_path)
    resolved = resolve(env.settings, env.db, env.vault, uid)
    assert (resolved.kind, resolved.source, resolved.notice) == ("youtube", "server", None)


def test_resolve_server_default_webdav_comes_from_settings(tmp_path):
    env, uid = user(tmp_path, storage="webdav", storage_webdav_url="https://nc.example/dav", storage_webdav_user="srv",
                    storage_webdav_password="pw", storage_webdav_folder="Tapo")
    resolved = resolve(env.settings, env.db, env.vault, uid)
    assert resolved.kind == "webdav" and resolved.source == "server"
    assert resolved.config == {"url": "https://nc.example/dav", "user": "srv", "password": "pw", "folder": "Tapo"}


def test_resolve_uses_own_webdav_when_allowed(tmp_path):
    env, uid = user(tmp_path, allow_own_storage=True)
    save_own(env, uid)
    resolved = resolve(env.settings, env.db, env.vault, uid)
    assert (resolved.kind, resolved.source, resolved.notice) == ("webdav", "own", None)
    assert resolved.config["url"] == OWN["url"] and resolved.config["password"] == "S3cretPW"


def test_resolve_own_youtube_choice_overrides_a_webdav_server_default(tmp_path):
    env, uid = user(tmp_path, allow_own_storage=True, storage="webdav", storage_webdav_url="https://nc.example/dav")
    save_own(env, uid, "youtube", None)
    assert resolve(env.settings, env.db, env.vault, uid).kind == "youtube"


def test_resolve_ignores_own_when_allow_own_storage_off(tmp_path):
    env, uid = user(tmp_path)
    save_own(env, uid)
    resolved = resolve(env.settings, env.db, env.vault, uid)
    assert resolved.kind == "youtube" and resolved.source == "server" and "not allowed" in resolved.notice


def test_resolve_falls_back_with_notice_when_kind_removed_from_allowed(tmp_path):
    env, uid = user(tmp_path, allow_own_storage=True, storage_allowed=("youtube",))
    save_own(env, uid)
    resolved = resolve(env.settings, env.db, env.vault, uid)
    assert resolved.kind == "youtube" and "webdav" in resolved.notice and "server default" in resolved.notice


def test_resolve_never_allows_rclone_for_users_and_never_a_path_for_local(tmp_path):
    env, uid = user(tmp_path, allow_own_storage=True, storage_allowed=("local", "rclone", "youtube"))
    save_own(env, uid, "rclone", {"root": "/etc"})
    resolved = resolve(env.settings, env.db, env.vault, uid)
    assert resolved.kind == "youtube" and resolved.source == "server" and resolved.notice
    save_own(env, uid, "local", {"root": "/etc"})            # a saved path is never honoured: only the server's own folder
    resolved = resolve(env.settings, env.db, env.vault, uid)
    assert resolved.kind == "local" and resolved.config == {} and resolved.source == "own"


def test_resolve_unreadable_saved_config_falls_back_with_notice(tmp_path):
    env, uid = user(tmp_path, allow_own_storage=True)
    env.db.save_storage_config(uid, "webdav", "not-a-valid-token")
    resolved = resolve(env.settings, env.db, env.vault, uid)
    assert resolved.kind == "youtube" and "cannot be read" in resolved.notice


def test_signature_changes_with_config_and_hides_the_password(tmp_path):
    env, uid = user(tmp_path, allow_own_storage=True)
    save_own(env, uid)
    first = resolve(env.settings, env.db, env.vault, uid).signature()
    save_own(env, uid, config={**OWN, "password": "another"})
    second = resolve(env.settings, env.db, env.vault, uid).signature()
    assert first != second and "S3cretPW" not in first and "another" not in second


def ctx_for(tmp_path, uid=7, cid=3):
    return SimpleNamespace(data_dir=tmp_path / "users" / str(uid) / f"camera-{cid}", playlist_prefix="Tapo")


def test_build_youtube_is_none(tmp_path):
    env, uid = user(tmp_path)
    assert build(resolve(env.settings, env.db, env.vault, uid), env.settings, ctx_for(tmp_path)) is None


def test_build_server_default_destinations_are_separated_per_camera(tmp_path):
    root = tmp_path / "videos"
    env, uid = user(tmp_path, storage="local", storage_local_root=str(root))
    storage = build(resolve(env.settings, env.db, env.vault, uid), env.settings, ctx_for(tmp_path))
    assert isinstance(storage, LocalStorage) and storage.root == root / "7" / "camera-3"
    env2, uid2 = user(tmp_path / "b", storage="webdav", storage_webdav_url="https://nc.example/dav",
                      storage_webdav_folder="Tapo")
    dav = build(resolve(env2.settings, env2.db, env2.vault, uid2), env2.settings, ctx_for(tmp_path))
    assert isinstance(dav, WebDAVStorage) and dav.folder == ["Tapo", "7", "camera-3"] and dav.host_check is None


def test_build_own_webdav_keeps_the_users_folder_and_checks_the_host(tmp_path):
    env, uid = user(tmp_path, allow_own_storage=True)
    save_own(env, uid)
    dav = build(resolve(env.settings, env.db, env.vault, uid), env.settings, ctx_for(tmp_path), public_resolver)
    assert dav.folder == ["Tapo"] and dav.host_check("cloud.friend.example") == "93.184.216.34"


def private_resolver(host, port=None, *args, **kwargs):
    import socket
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]


def test_build_own_webdav_rejects_private_host_unless_allowed(tmp_path):
    env, uid = user(tmp_path, allow_own_storage=True)
    save_own(env, uid)
    dav = build(resolve(env.settings, env.db, env.vault, uid), env.settings, ctx_for(tmp_path), private_resolver)
    with pytest.raises(Exception, match="non-public"):
        dav.host_check("cloud.friend.example")
    env2, uid2 = user(tmp_path / "b", allow_own_storage=True, allow_private_hosts=True)
    save_own(env2, uid2)
    lan = build(resolve(env2.settings, env2.db, env2.vault, uid2), env2.settings, ctx_for(tmp_path), private_resolver)
    assert lan.host_check("cloud.friend.example") == "10.0.0.5"


# ----- supervisor wiring -----

@pytest.fixture(autouse=True)
def reset_workers():
    FakeWorker.instances = []


def test_supervisor_gives_worker_the_resolved_storage_and_no_gate(tmp_path):
    env = make(tmp_path, storage="local", storage_local_root=str(tmp_path / "vid"))
    cid = add_camera(env, env.alice)
    env.sup.sync()
    worker, = FakeWorker.instances
    assert isinstance(worker.ctx.storage, LocalStorage) and worker.gate is None
    assert worker.ctx.storage.root == tmp_path / "vid" / str(env.alice) / f"camera-{cid}"


def test_youtube_cameras_keep_the_gate_and_the_stagger_only_among_themselves(tmp_path):
    env = make(tmp_path, allow_own_storage=True, upload_stagger_minutes=10)
    first = add_camera(env, env.alice, "a")
    second = add_camera(env, env.bob, "b")
    save_own(env, env.alice)           # alice uses her own WebDAV, bob stays on YouTube
    env.sup.sync()
    by_name = {w.ctx.name: w for w in FakeWorker.instances}
    assert by_name[f"camera-{first}"].gate is None and by_name[f"camera-{first}"].ctx.upload_at == dtime(21, 0)
    assert by_name[f"camera-{second}"].gate is not None and by_name[f"camera-{second}"].ctx.upload_at == dtime(21, 0)


def test_changing_destination_restarts_worker_and_pending_clips_are_kept(tmp_path):
    from tapo_web.cameras import camera_data_dir
    env = make(tmp_path, allow_own_storage=True)
    cid = add_camera(env, env.alice)
    env.sup.sync()
    old = FakeWorker.instances[0]
    clip = camera_data_dir(env.settings, env.db.camera_by_id(cid)) / "events" / "20261026" / "120000.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"footage")
    save_own(env, env.alice)
    env.sup.sync()
    assert old.stopped and len(FakeWorker.instances) == 2
    new = FakeWorker.instances[1]
    assert isinstance(new.ctx.storage, WebDAVStorage) and clip.read_bytes() == b"footage"


def test_unusable_storage_marks_the_camera_and_starts_nothing(tmp_path):
    env = make(tmp_path, storage="rclone", rclone_remote="a b")   # not a valid rclone remote name
    cid = add_camera(env, env.alice)
    env.sup.sync()
    assert FakeWorker.instances == []
    camera = env.db.camera_by_id(cid)
    assert camera["status"] == "error" and "remote name" in camera["last_error"]


def stale_clip(env, uid, cid):
    from tapo_web.cameras import camera_data_dir
    clip = camera_data_dir(env.settings, env.db.camera_by_id(cid)) / "events" / "20200101" / "120000.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"x")
    old = env.clock() - 30 * 86400
    os.utime(clip, (old, old))
    return clip


def test_maintenance_keeps_stale_clips_for_non_youtube_users_without_link(tmp_path):
    env = make(tmp_path, storage="local", storage_local_root=str(tmp_path / "vid"))
    env.clock.now = time.time()
    cid = add_camera(env, env.alice)
    clip = stale_clip(env, env.alice, cid)
    env.sup.maintenance(datetime.fromtimestamp(env.clock()))
    assert clip.exists()


def test_user_without_youtube_link_on_youtube_still_discards_stale(tmp_path):
    env = make(tmp_path)
    env.clock.now = time.time()
    cid = add_camera(env, env.alice)
    clip = stale_clip(env, env.alice, cid)
    env.sup.maintenance(datetime.fromtimestamp(env.clock()))
    assert not clip.exists()


def test_resolve_ignores_a_saved_config_that_is_not_an_object(tmp_path):
    env, uid = user(tmp_path, allow_own_storage=True)
    env.db.save_storage_config(uid, "webdav", env.vault.encrypt("[1, 2, 3]"))
    resolved = resolve(env.settings, env.db, env.vault, uid)
    assert resolved.kind == "youtube" and resolved.source == "server" and "cannot be read" in resolved.notice
