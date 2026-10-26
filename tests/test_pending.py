"""Clips that have not been uploaded yet can be watched from the personal page, marked as not uploaded."""
import os
import re
import time

import pytest

from tapo_web.cameras import camera_data_dir
from tests.conftest import Env
from tests.test_dashboard import FakeYoutube, item, link, playlist

DAY = "20261026"


def make(tmp_path, **settings):
    env = Env(tmp_path, max_cameras_per_user=2, **settings)
    env.alice = env.add_local_user("alice", "correct horse")
    env.cid = env.db.add_camera(env.alice, "Garden", "cam.example.com", 38554, 38020, "stream1",
                                env.vault.encrypt("u"), env.vault.encrypt("p"))
    env.client = env.app.test_client()
    env.login("alice", "correct horse", client=env.client)
    return env


def clip(env, camera_id, name="101500.mp4", data=b"0123456789", age=600, day=DAY):
    camera = env.db.camera_by_id(camera_id)
    path = camera_data_dir(env.settings, camera) / "events" / day / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def url(cid, name="101500.mp4", day=DAY):
    return f"/pending/{cid}/{day}/{name}"


def test_owner_gets_the_pending_clip_with_range(tmp_path):
    env = make(tmp_path)
    clip(env, env.cid)
    full = env.client.get(url(env.cid))
    assert full.status_code == 200 and full.data == b"0123456789"
    assert full.headers["Content-Type"] == "video/mp4" and full.headers["Content-Length"] == "10"
    assert full.headers["Accept-Ranges"] == "bytes" and "no-store" in full.headers["Cache-Control"]
    part = env.client.get(url(env.cid), headers={"Range": "bytes=2-5"})
    assert part.status_code == 206 and part.data == b"2345" and part.headers["Content-Range"] == "bytes 2-5/10"
    assert env.client.get(url(env.cid), headers={"Range": "bytes=-3"}).data == b"789"


@pytest.mark.parametrize("spec", ["bytes=abc", "bytes=20-30", "bytes=0-1,3-4"])
def test_unsatisfiable_range_is_416(tmp_path, spec):
    env = make(tmp_path)
    clip(env, env.cid)
    response = env.client.get(url(env.cid), headers={"Range": spec})
    assert response.status_code == 416 and response.headers["Content-Range"] == "bytes */10"


def test_someone_elses_camera_is_404_and_an_admin_may_watch(tmp_path):
    env = make(tmp_path)
    clip(env, env.cid)
    env.add_local_user("bob", "another pass")
    env.add_local_user("root", "admin pass", role="admin")
    bob, admin = env.app.test_client(), env.app.test_client()
    env.login("bob", "another pass", client=bob)
    env.login("root", "admin pass", client=admin)
    assert bob.get(url(env.cid)).status_code == 404
    assert admin.get(url(env.cid)).data == b"0123456789"


def test_requires_login(tmp_path):
    env = make(tmp_path)
    clip(env, env.cid)
    assert env.app.test_client().get(url(env.cid)).status_code == 302


@pytest.mark.parametrize("path", ["{cid}/20261026/..%2F101500.mp4", "{cid}/20261026/..\\101500.mp4", "{cid}/..%2F../101500.mp4",
                                  "{cid}/20261026/101500.mp4%00.png", "{cid}/20261026/day.mp4", "{cid}/20261026/101500.mp4.part",
                                  "{cid}/20261026/999999.mp4", "{cid}/2026-10-26/101500.mp4", "{cid}/202610/101500.mp4",
                                  "{cid}/20261026/%2Fetc%2Fpasswd", "999/20261026/101500.mp4"])
def test_hostile_or_unknown_names_are_404(tmp_path, path):
    env = make(tmp_path)
    clip(env, env.cid)
    clip(env, env.cid, "day.mp4")
    assert env.client.get("/pending/" + path.format(cid=env.cid)).status_code == 404


def test_a_link_to_somewhere_else_is_not_followed(tmp_path):
    env = make(tmp_path)
    target = tmp_path / "secret.mp4"
    target.write_bytes(b"secret")
    link_path = clip(env, env.cid).with_name("111111.mp4")
    try:
        link_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert env.client.get(url(env.cid, "111111.mp4")).status_code == 404


def test_page_lists_waiting_clips_as_not_uploaded(tmp_path):
    env = make(tmp_path)
    clip(env, env.cid, "101500.mp4")
    clip(env, env.cid, "093000.mp4", day="20261025")
    clip(env, env.cid, "day.mp4")                         # the joined day is not a waiting clip
    text = env.client.get("/").get_data(as_text=True)
    assert "Waiting for upload" in text and text.count("Not uploaded yet") == 2
    assert 'data-ref="/pending/%d/20261026/101500.mp4"' % env.cid in text
    assert 'data-ref="/pending/%d/20261025/093000.mp4"' % env.cid in text
    assert "day.mp4" not in text
    assert text.index("Motion at 10:15") < text.index("Motion at 09:30")          # newest first
    cards = re.findall(r'<div class="video-card".*?class="video-actions">(.*?)</div>', text, re.S)
    assert len(cards) == 2 and all("Watch here" in c and "Open the video" not in c for c in cards)


def test_a_clip_still_being_recorded_is_not_offered(tmp_path):
    env = make(tmp_path)
    clip(env, env.cid, "101500.mp4", age=5)
    assert "Waiting for upload" not in env.client.get("/").get_data(as_text=True)


def test_no_section_when_nothing_waits(tmp_path):
    env = make(tmp_path)
    text = env.client.get("/").get_data(as_text=True)
    assert "Waiting for upload" not in text and "Not uploaded yet" not in text


def test_waiting_clips_show_even_when_youtube_is_down_or_not_linked(tmp_path):
    env = make(tmp_path)
    clip(env, env.cid)
    link(env, env.alice, "token-alice")

    def broken(credentials):
        raise RuntimeError("youtube is down")

    env.app.extensions["tapo"]["youtube_builder"] = broken
    text = env.client.get("/").get_data(as_text=True)
    assert "Waiting for upload" in text and "did not answer" in text


def test_waiting_clips_show_next_to_a_file_destination(tmp_path):
    env = make(tmp_path, storage="local", storage_local_root=str(tmp_path / "videos"))
    clip(env, env.cid)
    assert "Waiting for upload" in env.client.get("/").get_data(as_text=True)


def test_with_two_cameras_the_group_says_which_one(tmp_path):
    env = make(tmp_path)
    second = env.db.add_camera(env.alice, "Door", "cam.example.com", 38554, 38020, "stream1",
                               env.vault.encrypt("u"), env.vault.encrypt("p"))
    clip(env, env.cid)
    clip(env, second, "120000.mp4")
    text = env.client.get("/").get_data(as_text=True)
    assert "Garden · " in text and "Door · " in text and text.count("Not uploaded yet") == 2


def test_never_lists_another_users_clips(tmp_path):
    env = make(tmp_path)
    env.add_local_user("bob", "another pass")
    bob_cam = env.db.add_camera(env.db.user_by_username("local", "bob")["id"], "Bobcam", "cam.example.com", 38554, 38020,
                                "stream1", env.vault.encrypt("u"), env.vault.encrypt("p"))
    clip(env, bob_cam, "120000.mp4")
    assert "Waiting for upload" not in env.client.get("/").get_data(as_text=True)
