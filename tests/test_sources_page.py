"""The personal page lists the videos of every source, each marked with where it lives."""
import re

from tapo_storage.local import LocalStorage
from tapo_web.cameras import camera_data_dir
from tapo_web.sources import remember
from tapo_web.storage import Resolved, build
from tests.conftest import Env
from tests.test_dashboard import FakeYoutube, item, link, playlist

DAY = "2026-10-26"


def setup(tmp_path, **settings):
    env = Env(tmp_path, storage="local", storage_local_root=str(tmp_path / "current"), **settings)
    env.alice = env.add_local_user("alice", "correct horse")
    env.cid = env.db.add_camera(env.alice, "Garden", "cam.example.com", 38554, 38020, "stream1",
                                env.vault.encrypt("u"), env.vault.encrypt("p"))
    env.client = env.app.test_client()
    env.login("alice", "correct horse", client=env.client)
    return env


def put(env, resolved, title, data=b"x" * 2048):
    camera = env.db.camera_by_id(env.cid)
    ctx = type("Ctx", (), {"data_dir": camera_data_dir(env.settings, camera), "playlist_prefix": "Tapo"})()
    source = env.settings.data_dir / "src.mp4"
    source.write_bytes(data)
    return build(resolved, env.settings, ctx, env.app.extensions["tapo"]["resolver"]).put(source, DAY, title, "")


def old_source(env, root_name="old", title="from the old place"):
    """A destination the user left: remembered, with one video still in it."""
    resolved = Resolved("local", {"root": str(env.settings.data_dir / root_name)}, "server")
    sid = remember(env.db, env.vault, env.alice, resolved)
    put(env, resolved, title)
    return sid, resolved


def page(env):
    return env.client.get("/").get_data(as_text=True)


def badges(text):
    return re.findall(r'<span class="badge badge-source">([^<]+)</span>', text)


def test_current_source_videos_carry_their_label(tmp_path):
    env = setup(tmp_path)
    put(env, Resolved("local", {"root": str(tmp_path / "current")}, "server"), "now clip")
    text = page(env)
    assert "now clip" in text and badges(text) == ["Server folder"]


def test_old_sources_stay_visible_with_their_own_label(tmp_path):
    env = setup(tmp_path)
    put(env, Resolved("local", {"root": str(tmp_path / "current")}, "server"), "now clip")
    sid, _ = old_source(env)
    text = page(env)
    assert "now clip" in text and "from the old place" in text
    assert badges(text) == ["Server folder", "Server folder"]
    assert f"/media/{env.cid}/{DAY}/from%20the%20old%20place.mp4?s={sid}" in text
    assert f"/media/{env.cid}/{DAY}/now%20clip.mp4?s=" not in text          # the current source needs no selector


def test_youtube_that_is_not_the_current_destination_still_shows(tmp_path):
    env = setup(tmp_path)
    link(env, env.alice, "token-alice")
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: FakeYoutube(
        [{"items": [playlist("P", f"Tapo {DAY}")]}], {"P": [item("abc123", "yt clip", privacy="unlisted")]})
    text = page(env)
    assert "yt clip" in text and "YouTube" in badges(text)


def test_youtube_is_listed_first_when_it_is_the_current_destination_and_the_old_source_follows(tmp_path):
    env = Env(tmp_path, max_cameras_per_user=1)
    alice = env.add_local_user("alice", "correct horse")
    cid = env.db.add_camera(alice, "Garden", "cam.example.com", 38554, 38020, "stream1",
                            env.vault.encrypt("u"), env.vault.encrypt("p"))
    link(env, alice, "token-alice")
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: FakeYoutube(
        [{"items": [playlist("P", f"Tapo {DAY}")]}], {"P": [item("abc123", "yt clip", privacy="unlisted")]})
    env.alice, env.cid = alice, cid
    sid, _ = old_source(env, title="old file clip")
    env.client = env.app.test_client()
    env.login("alice", "correct horse", client=env.client)
    text = page(env)
    assert text.index("yt clip") < text.index("old file clip") and badges(text) == ["YouTube", "Server folder"]


def test_a_source_that_does_not_answer_does_not_hide_the_others(tmp_path):
    env = setup(tmp_path)
    put(env, Resolved("local", {"root": str(tmp_path / "current")}, "server"), "now clip")
    remember(env.db, env.vault, env.alice, Resolved("webdav", {"url": "http://127.0.0.1:1/dav", "user": "u", "password": "p"}, "own"))
    text = page(env)
    assert "now clip" in text and "WebDAV" in text and "could not load" in text


def test_the_old_source_video_plays_through_media_with_range(tmp_path):
    env = setup(tmp_path)
    sid, resolved = old_source(env)
    name = "from%20the%20old%20place.mp4"
    full = env.client.get(f"/media/{env.cid}/{DAY}/{name}?s={sid}")
    assert full.status_code == 200 and len(full.data) == 2048
    part = env.client.get(f"/media/{env.cid}/{DAY}/{name}?s={sid}", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.data) == 10
    assert env.client.get(f"/media/{env.cid}/{DAY}/{name}").status_code == 404      # not in the current source


def test_somebody_elses_source_id_or_garbage_is_404(tmp_path):
    env = setup(tmp_path)
    sid, _ = old_source(env)
    bob = env.add_local_user("bob", "another pass")
    env.db.add_camera(bob, "Bobcam", "cam.example.com", 38554, 38020, "stream1", env.vault.encrypt("u"), env.vault.encrypt("p"))
    other = env.app.test_client()
    env.login("bob", "another pass", client=other)
    name = "from%20the%20old%20place.mp4"
    assert other.get(f"/media/{env.cid}/{DAY}/{name}?s={sid}").status_code == 404
    for bad in ("abc", "999", "-1", "1e3", ""):
        assert env.client.get(f"/media/{env.cid}/{DAY}/{name}?s={bad}").status_code == 404


def test_the_storage_page_lists_sources_and_forgetting_one_deletes_no_video(tmp_path):
    env = setup(tmp_path, allow_own_storage=True)
    sid, resolved = old_source(env)
    text = env.client.get("/storage").get_data(as_text=True)
    assert "Server folder" in text and f"/storage/sources/{sid}/forget" in text
    response = env.client.post(f"/storage/sources/{sid}/forget", data={"csrf_token": env.csrf(env.client, "/storage")})
    assert response.status_code == 302 and env.db.storage_sources(env.alice) == []
    root = env.settings.data_dir / "old"
    assert list(root.rglob("*.mp4"))                                          # the file is still there
    assert "from the old place" not in page(env)


def test_one_cannot_forget_somebody_elses_source(tmp_path):
    env = setup(tmp_path, allow_own_storage=True)
    sid, _ = old_source(env)
    env.add_local_user("bob", "another pass")
    other = env.app.test_client()
    env.login("bob", "another pass", client=other)
    token = env.csrf(other, "/storage")
    assert other.post(f"/storage/sources/{sid}/forget", data={"csrf_token": token}).status_code == 404
    assert len(env.db.storage_sources(env.alice)) == 1
