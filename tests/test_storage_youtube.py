"""YouTube as one storage backend, and the daily uploader talking only to a storage object."""
import threading
from datetime import datetime

import pytest

from tapo_storage.base import Storage, StorageError, StoredItem


class RecordingStorage(Storage):
    kind = "fake"
    uses_quota = False

    def __init__(self, error=None):
        self.puts, self.error = [], error

    def put(self, path, day, title, description=""):
        if self.error:
            raise self.error
        self.puts.append((path.name, day, title, description))
        return StoredItem(title, day, 1, "", None)


class ForbiddenGate:
    def allowed(self, *args):
        raise AssertionError("the quota gate must not be asked for a storage without quota")

    def charge(self, *args):
        raise AssertionError("the quota gate must not be charged for a storage without quota")

    def exhausted(self, *args):
        raise AssertionError("no quota to exhaust")


def make_ctx(tmp_path, **extra):
    import camera_context
    return camera_context.CameraContext(name="c", host="h", data_dir=tmp_path, **extra)


def due_clip(ctx):
    ctx.ensure_dirs()
    day = ctx.events_dir / "20200101"
    day.mkdir(parents=True)
    clip = day / "120000.mp4"
    clip.write_bytes(b"x")
    return day, clip


def patch_media(motion, monkeypatch):
    monkeypatch.setattr(motion, "join", lambda parts, out: out.write_bytes(b"joined"))
    monkeypatch.setattr(motion, "clip_seconds", lambda path: 10.0)


def test_youtube_put_delegates_to_base_upload_with_day_start(single_user_env, tmp_path, monkeypatch):
    from tapo_storage.youtube import YouTubeStorage
    calls = []
    monkeypatch.setattr(single_user_env.uploader, "upload", lambda *args, **kw: calls.append((args, kw)))
    client = object()
    ctx = make_ctx(tmp_path, youtube=lambda: client)
    state = {"playlists": {}}
    storage = YouTubeStorage(ctx, state)
    storage.put(tmp_path / "day.mp4", "2026-10-26", "A title", "chapters")
    (args, kw), = calls
    assert args[0] is ctx and args[1] is client and args[2] is state and args[3] == tmp_path / "day.mp4"
    assert args[4] == datetime(2026, 10, 26) and kw == {"title": "A title", "description": "chapters"}
    assert storage.kind == "youtube" and storage.uses_quota


def test_youtube_check_wraps_failures_in_storage_error(single_user_env, tmp_path):
    from tapo_storage.youtube import YouTubeStorage

    def broken():
        raise RuntimeError("no token")

    with pytest.raises(StorageError, match="no token"):
        YouTubeStorage(make_ctx(tmp_path, youtube=broken), {}).check()


def test_storage_for_defaults_to_youtube(single_user_env, tmp_path):
    from tapo_storage.youtube import YouTubeStorage
    assert isinstance(single_user_env.motion.storage_for(make_ctx(tmp_path)), YouTubeStorage)


def test_storage_for_uses_context_storage(single_user_env, tmp_path):
    fake = RecordingStorage()
    assert single_user_env.motion.storage_for(make_ctx(tmp_path, storage=fake)) is fake


def test_upload_pending_uses_storage_put_and_skips_gate_when_not_quota(single_user_env, tmp_path, monkeypatch):
    motion = single_user_env.motion
    patch_media(motion, monkeypatch)
    fake = RecordingStorage()
    ctx = make_ctx(tmp_path, storage=fake)
    day, clip = due_clip(ctx)
    seen = []
    motion.upload_pending(ctx, {"playlists": {}}, report=lambda kind, detail="": seen.append(kind), gate=ForbiddenGate())
    (name, day_arg, title, description), = fake.puts
    assert name == "day.mp4" and day_arg == "2020-01-01" and title.startswith("Tapo 2020-01-01 eventi")
    assert "Movimento alle 12:00:00" in description
    assert not clip.exists() and seen == ["uploaded"]


def test_daily_loop_reports_storage_error_without_quota_handling(single_user_env, tmp_path, monkeypatch):
    motion = single_user_env.motion
    patch_media(motion, monkeypatch)
    ctx = make_ctx(tmp_path, storage=RecordingStorage(StorageError("remote said no")))
    day, clip = due_clip(ctx)
    stop = threading.Event()
    waits = []
    stop.wait = lambda seconds=None: (waits.append(seconds), stop.set())[0]
    seen = []
    motion.daily_loop(ctx, stop, lambda kind, detail="": seen.append((kind, detail)), ForbiddenGate())
    assert ("error", "remote said no") in seen and clip.exists() and waits == [600]


def test_unreachable_destination_is_found_before_clips_are_joined(single_user_env, tmp_path, monkeypatch):
    motion = single_user_env.motion
    joined = []
    monkeypatch.setattr(motion, "join", lambda parts, out: joined.append(out))

    class Down(RecordingStorage):
        def connect(self):
            raise StorageError("server down")

    ctx = make_ctx(tmp_path, storage=Down())
    due_clip(ctx)
    with pytest.raises(StorageError):
        motion.upload_pending(ctx, {"playlists": {}})
    assert joined == []
