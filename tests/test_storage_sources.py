"""Where recordings used to go stays listed: every destination a user has used is remembered as a source."""
import json

import pytest

from tapo_web.db import Database
from tapo_web.sources import label_for, remember, remembered
from tapo_web.storage import Resolved
from tests.conftest import Env

GOOD = {"kind": "webdav", "url": "https://cloud.friend.example/remote.php/dav/files/me", "user": "me",
        "password": "S3cretPW", "folder": "Tapo"}


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()
    return database


def test_sources_table_roundtrip_dedupe_and_delete(db):
    uid = db.add_user("local", "alice", "Alice", "user", "hash")
    assert db.storage_sources(uid) == []
    first = db.add_storage_source(uid, "sig-1", "webdav", "own", "enc-1", "WebDAV")
    assert db.add_storage_source(uid, "sig-1", "webdav", "own", "enc-1", "WebDAV") == first   # same destination: one row
    db.add_storage_source(uid, "sig-2", "youtube", "server", None, "YouTube")
    assert [r["signature"] for r in db.storage_sources(uid)] == ["sig-1", "sig-2"]
    assert db.storage_source(uid, first)["label"] == "WebDAV"
    other = db.add_user("local", "bob", "Bob", "user", "hash")
    assert db.storage_source(other, first) is None and db.storage_sources(other) == []
    db.delete_storage_source(uid, first)
    assert [r["signature"] for r in db.storage_sources(uid)] == ["sig-2"]


def test_deleting_the_user_removes_the_sources(db):
    uid = db.add_user("local", "alice", "Alice", "user", "hash")
    sid = db.add_storage_source(uid, "sig", "webdav", "own", "enc", "WebDAV")
    db.delete_user(uid)
    assert db.storage_source(uid, sid) is None


def test_labels_name_the_kind_and_the_host():
    assert label_for(Resolved("youtube")) == "YouTube"
    assert label_for(Resolved("onedrive", {"refresh_token": "x"}, "own")) == "OneDrive"
    assert label_for(Resolved("webdav", {"url": "https://cloud.friend.example/dav"}, "own")) == "WebDAV · cloud.friend.example"
    assert label_for(Resolved("webdav", {"url": "https://srv.example/dav"}, "server")) == "WebDAV · srv.example"
    assert label_for(Resolved("local", {"root": "/data/v"}, "server")) == "Server folder"
    assert label_for(Resolved("rclone", {"remote": "mega"}, "server")) == "Cloud storage (rclone)"


def test_remember_keeps_the_settings_encrypted_and_gives_them_back(tmp_path):
    env = Env(tmp_path)
    uid = env.add_local_user("alice", "correct horse")
    old = Resolved("webdav", {"url": "https://cloud.friend.example/dav", "user": "me", "password": "S3cretPW", "folder": "T"}, "own")
    remember(env.db, env.vault, uid, old)
    remember(env.db, env.vault, uid, old)                                   # again: still one
    raw = env.db.storage_sources(uid)
    assert len(raw) == 1 and "S3cretPW" not in raw[0]["config_enc"] and "cloud.friend.example" not in raw[0]["config_enc"]
    [(source_id, back)] = remembered(env.db, env.vault, uid)
    assert source_id == raw[0]["id"] and back.kind == "webdav" and back.config == old.config and back.source == "own"


def test_a_source_that_cannot_be_read_is_skipped_not_fatal(tmp_path):
    env = Env(tmp_path)
    uid = env.add_local_user("alice", "correct horse")
    env.db.add_storage_source(uid, "sig", "webdav", "own", "not-encrypted-at-all", "WebDAV")
    remember(env.db, env.vault, uid, Resolved("youtube"))
    assert [r.kind for _, r in remembered(env.db, env.vault, uid)] == ["youtube"]


def setup(tmp_path, **settings):
    env = Env(tmp_path, allow_own_storage=True, **settings)
    env.alice = env.add_local_user("alice", "correct horse")
    env.client = env.app.test_client()
    env.login("alice", "correct horse", client=env.client)
    return env


def post(env, **fields):
    return env.client.post("/storage", data={"csrf_token": env.csrf(env.client, "/storage"), **fields})


def kinds(env):
    return [(r["kind"], r["origin"]) for r in env.db.storage_sources(env.alice)]


def test_switching_from_the_server_default_remembers_it(tmp_path):
    env = setup(tmp_path)
    assert post(env, **GOOD).status_code == 302
    assert kinds(env) == [("youtube", "server")]


def test_switching_between_own_destinations_remembers_each(tmp_path):
    env = setup(tmp_path)
    post(env, **GOOD)
    post(env, **{**GOOD, "url": "https://other.friend.example/dav"})
    post(env, kind="youtube")
    assert kinds(env) == [("youtube", "server"), ("webdav", "own"), ("webdav", "own")]


def test_saving_the_same_destination_again_remembers_nothing_new(tmp_path):
    env = setup(tmp_path)
    post(env, **GOOD)
    post(env, **GOOD)
    assert kinds(env) == [("youtube", "server")]


def test_going_back_to_the_default_remembers_the_own_one(tmp_path):
    env = setup(tmp_path)
    post(env, **GOOD)
    post(env, kind="default")
    assert ("webdav", "own") in kinds(env)
    stored = json.dumps([dict(r) for r in env.db.storage_sources(env.alice)])
    assert "S3cretPW" not in stored


def test_a_refused_change_remembers_nothing(tmp_path):
    env = setup(tmp_path)
    assert post(env, **{**GOOD, "url": "ftp://nope"}).status_code == 400
    assert kinds(env) == []
