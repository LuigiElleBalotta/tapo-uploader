"""Server-level storage settings and the per-user storage table."""
import sqlite3

import pytest

from tapo_web.config import Settings, SettingsError
from tapo_web.db import Database


def test_storage_settings_defaults():
    settings = Settings.from_env({})
    assert settings.storage == "youtube" and settings.storage_allowed == ("youtube", "webdav", "onedrive")
    assert settings.allow_own_storage is False
    assert settings.storage_webdav_url == "" and settings.storage_local_root == "" and settings.rclone_remote == ""


def test_storage_values_are_read():
    settings = Settings.from_env({
        "STORAGE": "webdav", "STORAGE_WEBDAV_URL": "https://cloud.example.com/remote.php/dav/files/srv",
        "STORAGE_WEBDAV_USER": "srv", "STORAGE_WEBDAV_PASSWORD": "pw", "STORAGE_WEBDAV_FOLDER": "Tapo",
        "STORAGE_ALLOWED": "webdav", "ALLOW_OWN_STORAGE": "true"})
    assert settings.storage == "webdav" and settings.storage_allowed == ("webdav",) and settings.allow_own_storage
    assert (settings.storage_webdav_user, settings.storage_webdav_password, settings.storage_webdav_folder) == ("srv", "pw", "Tapo")
    local = Settings.from_env({"STORAGE": "local", "STORAGE_LOCAL_ROOT": "/data/videos"})
    assert local.storage_local_root == "/data/videos"
    rclone = Settings.from_env({"STORAGE": "rclone", "STORAGE_RCLONE_REMOTE": "mega", "STORAGE_RCLONE_CONFIG": "/etc/r.conf",
                                "STORAGE_RCLONE_FOLDER": "cams"})
    assert (rclone.rclone_remote, rclone.rclone_config, rclone.rclone_folder) == ("mega", "/etc/r.conf", "cams")


@pytest.mark.parametrize("env", [{"STORAGE": "floppy"}, {"STORAGE_ALLOWED": "youtube,floppy"},
                                 {"STORAGE_ALLOWED": "rclone"}, {"STORAGE_ALLOWED": "rclone,webdav"}])
def test_unknown_or_server_only_kinds_are_rejected(env):
    with pytest.raises(SettingsError):
        Settings.from_env(env)


@pytest.mark.parametrize("env", [{"STORAGE": "webdav"}, {"STORAGE": "local"}, {"STORAGE": "rclone"}])
def test_server_default_without_its_value_is_rejected(env):
    with pytest.raises(SettingsError):
        Settings.from_env(env)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()
    return database


def test_storage_config_roundtrip_and_delete(db):
    uid = db.add_user("local", "alice", "Alice", "user", "hash")
    assert db.storage_config(uid) is None
    db.save_storage_config(uid, "webdav", "enc-1")
    row = db.storage_config(uid)
    assert row["kind"] == "webdav" and row["config_enc"] == "enc-1" and row["updated_at"] > 0
    db.save_storage_config(uid, "youtube", None)
    assert db.storage_config(uid)["kind"] == "youtube" and db.storage_config(uid)["config_enc"] is None
    db.delete_storage_config(uid)
    assert db.storage_config(uid) is None


def test_deleting_user_removes_storage_config(db):
    uid = db.add_user("local", "alice", "Alice", "user", "hash")
    db.save_storage_config(uid, "webdav", "enc")
    db.delete_user(uid)
    assert db.storage_config(uid) is None


def test_storage_config_is_per_user(db):
    a = db.add_user("local", "a", "A", "user", "h")
    b = db.add_user("local", "b", "B", "user", "h")
    db.save_storage_config(a, "webdav", "enc-a")
    assert db.storage_config(b) is None


def test_migration_from_version_1_adds_table_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    first = Database(path)
    first.migrate()
    uid = first.add_user("local", "alice", "Alice", "admin", "hash")
    first.save_youtube_link(uid, "tok", "Channel", "shared")
    first._conn.executescript("DROP TABLE storage_configs; UPDATE schema_version SET version = 1;")
    first._conn.close()
    second = Database(path)
    second.migrate()
    assert second.user_by_username("local", "alice")["id"] == uid
    assert second.youtube_link(uid)["channel_title"] == "Channel"
    second.save_storage_config(uid, "webdav", "enc")
    assert second.storage_config(uid)["kind"] == "webdav"
    assert second._conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert second._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
