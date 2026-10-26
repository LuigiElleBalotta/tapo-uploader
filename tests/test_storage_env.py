"""Single-user mode: STORAGE in .env chooses the destination, unset means YouTube exactly as before."""
import pytest

from tapo_storage.base import storage_from_env
from tapo_storage.local import LocalStorage
from tapo_storage.rclone import RcloneStorage
from tapo_storage.webdav import WebDAVStorage


class Ctx:
    playlist_prefix = "Cam"


def test_unset_or_youtube_means_no_storage_object():
    assert storage_from_env({}, Ctx()) is None
    assert storage_from_env({"STORAGE": "youtube"}, Ctx()) is None
    assert storage_from_env({"STORAGE": " YouTube "}, Ctx()) is None


def test_storage_webdav_builds_webdav_storage():
    env = {"STORAGE": "webdav", "WEBDAV_URL": "https://cloud.example.com/remote.php/dav/files/me",
           "WEBDAV_USER": "me", "WEBDAV_PASS": "pw", "WEBDAV_FOLDER": "Tapo"}
    storage = storage_from_env(env, Ctx())
    assert isinstance(storage, WebDAVStorage) and storage.prefix == "Cam" and storage.folder == ["Tapo"]


def test_storage_local_needs_local_root(tmp_path):
    with pytest.raises(SystemExit, match="LOCAL_ROOT"):
        storage_from_env({"STORAGE": "local"}, Ctx())
    storage = storage_from_env({"STORAGE": "local", "LOCAL_ROOT": str(tmp_path)}, Ctx())
    assert isinstance(storage, LocalStorage) and storage.root == tmp_path


def test_storage_rclone_needs_remote():
    with pytest.raises(SystemExit, match="RCLONE_REMOTE"):
        storage_from_env({"STORAGE": "rclone"}, Ctx())
    storage = storage_from_env({"STORAGE": "rclone", "RCLONE_REMOTE": "mega", "RCLONE_FOLDER": "cams"}, Ctx())
    assert isinstance(storage, RcloneStorage) and storage.folder == ["cams"]


def test_storage_webdav_needs_url():
    with pytest.raises(SystemExit, match="WEBDAV_URL"):
        storage_from_env({"STORAGE": "webdav"}, Ctx())


def test_unknown_storage_exits_with_clear_message():
    with pytest.raises(SystemExit, match="STORAGE.*floppy.*youtube, webdav, local or rclone"):
        storage_from_env({"STORAGE": "floppy"}, Ctx())


def test_bad_values_exit_instead_of_tracing_back():
    with pytest.raises(SystemExit, match="WEBDAV"):
        storage_from_env({"STORAGE": "webdav", "WEBDAV_URL": "ftp://nope"}, Ctx())


def test_context_from_env_carries_the_storage(single_user_env, monkeypatch, tmp_path):
    import camera_context
    monkeypatch.setenv("STORAGE", "local")
    monkeypatch.setenv("LOCAL_ROOT", str(tmp_path / "videos"))
    ctx = camera_context.CameraContext.from_env()
    assert isinstance(ctx.storage, LocalStorage) and ctx.storage.root == tmp_path / "videos"


def test_default_env_regression_stays_on_youtube(single_user_env):
    import camera_context
    from tapo_storage.youtube import YouTubeStorage
    ctx = camera_context.CameraContext.from_env()
    assert ctx.storage is None and isinstance(single_user_env.motion.storage_for(ctx), YouTubeStorage)
