import sqlite3
import time

import pytest

from tapo_web.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()
    return database


def _camera(db, user_id, name="cam"):
    return db.add_camera(user_id, name, "cam.example.com", 38554, 38020, "stream1", "enc-user", "enc-pass")


def test_user_roundtrip(db):
    uid = db.add_user("local", "alice", "Alice", "admin", "hash")
    user = db.user_by_username("local", "alice")
    assert user["id"] == uid and user["role"] == "admin" and user["display_name"] == "Alice"
    assert user["password_hash"] == "hash" and user["active"] == 1
    assert db.user_by_id(uid)["username"] == "alice"
    assert db.user_by_username("local", "nobody") is None
    assert [u["username"] for u in db.list_users()] == ["alice"]


def test_username_unique_per_origin(db):
    db.add_user("local", "alice", "Alice", "user")
    db.add_user("nextcloud", "alice", "Alice NC", "user")  # same name, other origin: fine
    with pytest.raises(sqlite3.IntegrityError):
        db.add_user("local", "alice", "Again", "user")


def test_camera_for_user_hides_other_users_camera(db):
    a = db.add_user("local", "a", "A", "user")
    b = db.add_user("local", "b", "B", "user")
    cam = _camera(db, a)
    assert db.camera_for_user(a, cam)["name"] == "cam"
    assert db.camera_for_user(b, cam) is None
    assert [c["id"] for c in db.cameras_for_user(a)] == [cam]
    assert db.cameras_for_user(b) == []
    assert db.delete_camera(b, cam) is False
    assert db.delete_camera(a, cam) is True


def test_cascade_delete_user_data(db):
    uid = db.add_user("local", "a", "A", "user")
    cam = _camera(db, uid)
    db.save_youtube_link(uid, "enc-token", "My channel", "shared")
    db.add_event(cam, "motion", "IsMotion")
    db.delete_user(uid)
    assert db.cameras_for_user(uid) == [] and db.youtube_link(uid) is None
    assert db.events_for_camera(cam) == []


def test_quota_accumulates_per_day(db):
    db.add_quota_use("2026-10-26", 1600)
    db.add_quota_use("2026-10-26", 50)
    db.add_quota_use("2026-10-27", 1)
    assert db.quota_used("2026-10-26") == 1650
    assert db.quota_used("2026-10-27") == 1
    assert db.quota_used("2026-10-28") == 0


def test_migrate_is_idempotent(db):
    uid = db.add_user("local", "a", "A", "user")
    db.migrate()
    db.migrate()
    assert db.user_by_id(uid)["username"] == "a"


def test_secrets_only_in_encrypted_columns(db):
    columns = lambda table: {row[1] for row in db._conn.execute(f"PRAGMA table_info({table})")}
    assert not ({"username", "password", "token"} & columns("cameras"))
    assert {"username_enc", "password_enc"} <= columns("cameras")
    assert not ({"token", "client_secret"} & columns("youtube_links"))
    assert {"token_enc", "client_secret_enc"} <= columns("youtube_links")


def test_all_enabled_skips_disabled_cameras_and_inactive_users(db):
    a = db.add_user("local", "a", "A", "user")
    b = db.add_user("local", "b", "B", "user")
    on, off, gone = _camera(db, a, "on"), _camera(db, a, "off"), _camera(db, b, "gone")
    db.set_camera_status(on, enabled=1)
    db.set_camera_status(gone, enabled=1)
    db.set_user_active(b, False)
    assert [c["id"] for c in db.all_enabled_cameras()] == [on]
    db.set_user_active(b, True)
    assert sorted(c["id"] for c in db.all_enabled_cameras()) == sorted([on, gone])


def test_set_camera_status_rejects_unknown_fields(db):
    uid = db.add_user("local", "a", "A", "user")
    cam = _camera(db, uid)
    with pytest.raises(ValueError):
        db.set_camera_status(cam, **{"name = 'x', enabled": 1})


def test_events_are_purged_by_age(db):
    uid = db.add_user("local", "a", "A", "user")
    cam = _camera(db, uid)
    db.add_event(cam, "motion", "old", ts=time.time() - 40 * 86400)
    db.add_event(cam, "motion", "new")
    db.purge_events(30)
    assert [e["detail"] for e in db.events_for_camera(cam)] == ["new"]


def test_update_user_profile(db):
    uid = db.add_user("nextcloud", "a", "Old Name", "user")
    db.update_user_profile(uid, "New Name", "admin")
    user = db.user_by_id(uid)
    assert user["display_name"] == "New Name" and user["role"] == "admin"


def test_update_youtube_token_keeps_link_data(db):
    uid = db.add_user("local", "a", "A", "user")
    db.save_youtube_link(uid, "enc-1", "My channel", "own", "enc-secret")
    linked = db.youtube_link(uid)["linked_at"]
    db.update_youtube_token(uid, "enc-2")
    link = db.youtube_link(uid)
    assert (link["token_enc"], link["channel_title"], link["client_source"]) == ("enc-2", "My channel", "own")
    assert link["client_secret_enc"] == "enc-secret" and link["linked_at"] == linked


def test_camera_count_and_ports(db):
    a = db.add_user("local", "a", "A", "user")
    c1 = db.add_camera(a, "one", "h", 40010, 40020, "stream1", "u", "p")
    db.add_camera(a, "two", "h", 40030, 40040, "stream1", "u", "p")
    assert db.camera_count() == 2
    assert db.camera_ports() == {40010, 40020, 40030, 40040}
    db.delete_camera(a, c1)
    assert db.camera_count() == 1 and db.camera_ports() == {40030, 40040}


def test_camera_by_id_is_for_the_supervisor_only(db):
    a = db.add_user("local", "a", "A", "user")
    cam = db.add_camera(a, "one", "h", 40010, 40020, "stream1", "u", "p")
    assert db.camera_by_id(cam)["user_id"] == a and db.camera_by_id(999) is None


def test_events_newest_first_even_with_equal_times(db):
    a = db.add_user("local", "a", "A", "user")
    cam = db.add_camera(a, "one", "h", 40010, 40020, "stream1", "u", "p")
    for kind in ("first", "second", "third"):
        db.add_event(cam, kind, "", ts=1000.0)
    assert [e["kind"] for e in db.events_for_camera(cam)] == ["third", "second", "first"]


def test_all_cameras_with_owner(db):
    a = db.add_user("local", "alice", "Alice A", "user")
    cam = db.add_camera(a, "Garden", "h", 40010, 40020, "stream1", "u", "p")
    rows = db.all_cameras_with_owner()
    assert [(r["id"], r["name"], r["owner_name"]) for r in rows] == [(cam, "Garden", "Alice A")]
