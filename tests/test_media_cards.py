"""Video cards on the personal page: "Watch here" and "Open the video"."""
import re

import pytest

from tapo_storage.local import LocalStorage
from tapo_web.cameras import camera_data_dir
from tapo_web.storage import build, resolve
from tests.conftest import Env
from tests.fakes import FakeDAV
from tests.test_dashboard import FakeYoutube, item, link, playlist

DAY = "2026-10-26"


def make(tmp_path, **settings):
    env = Env(tmp_path, max_cameras_per_user=2, **settings)
    env.alice = env.add_local_user("alice", "correct horse")
    env.bob = env.add_local_user("bob", "another pass")
    return env


def add_camera(env, user_id, name="Garden"):
    return env.db.add_camera(user_id, name, "cam.example.com", 38554, 38020, "stream1",
                             env.vault.encrypt("u"), env.vault.encrypt("p"))


def put_video(env, user_id, camera_id, title="clip one", description="00:00 x"):
    """Store a small video where this camera's server-default destination keeps it."""
    camera = env.db.camera_by_id(camera_id)
    ctx = type("Ctx", (), {"data_dir": camera_data_dir(env.settings, camera), "playlist_prefix": "Tapo"})()
    storage = build(resolve(env.settings, env.db, env.vault, user_id), env.settings, ctx, env.app.extensions["tapo"]["resolver"])
    source = env.settings.data_dir / "src.mp4"
    source.write_bytes(b"x" * 2048)
    return storage.put(source, DAY, title, description)


def page(env, username, password):
    client = env.app.test_client()
    env.login(username, password, client=client)
    return client.get("/")


def buttons(text):
    return re.findall(r'<a [^>]*>Open the video</a>', text)


def test_youtube_card_has_both_actions_and_embed_id(tmp_path):
    env = make(tmp_path)
    add_camera(env, env.alice)
    link(env, env.alice, "token-alice")
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: FakeYoutube(
        [{"items": [playlist("P", f"Tapo {DAY}")]}], {"P": [item("abc123", "clip", privacy="unlisted")]})
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert 'data-kind="youtube"' in text and 'data-ref="abc123"' in text and "Watch here" in text
    anchors = buttons(text)
    assert len(anchors) == 1 and 'href="https://www.youtube.com/watch?v=abc123"' in anchors[0]
    assert 'target="_blank"' in anchors[0] and 'rel="noopener noreferrer"' in anchors[0]


def test_local_card_has_only_watch_here(tmp_path):
    env = make(tmp_path, storage="local", storage_local_root=str(tmp_path / "videos"))
    cid = add_camera(env, env.alice)
    put_video(env, env.alice, cid)
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert 'data-kind="file"' in text and f'data-ref="/media/{cid}/{DAY}/clip%20one.mp4"' in text and "Watch here" in text
    assert buttons(text) == [] and "clip one" in text and "2.0 KB" in text and f"Tapo {DAY}" in text


def test_webdav_card_has_file_src_and_cloud_link(tmp_path):
    dav = FakeDAV()
    try:
        env = make(tmp_path, storage="webdav", storage_webdav_url=dav.url, storage_webdav_user="u", storage_webdav_password="p")
        cid = add_camera(env, env.alice)
        put_video(env, env.alice, cid)
        text = page(env, "alice", "correct horse").get_data(as_text=True)
        assert f'data-ref="/media/{cid}/{DAY}/clip%20one.mp4"' in text
        anchors = buttons(text)
        assert len(anchors) == 1 and f"{dav.url}/{env.alice}/camera-{cid}/Tapo%20{DAY}/clip%20one.mp4" in anchors[0]
    finally:
        dav.stop()


def test_players_are_not_in_the_initial_html(tmp_path):
    env = make(tmp_path, storage="local", storage_local_root=str(tmp_path / "videos"))
    cid = add_camera(env, env.alice)
    put_video(env, env.alice, cid)
    text = page(env, "alice", "correct horse").get_data(as_text=True).lower()
    assert "<iframe" not in text and "<video" not in text and "youtube-nocookie" not in text


def test_listing_failure_still_opens_the_page(tmp_path):
    dav = FakeDAV()
    env = make(tmp_path, storage="webdav", storage_webdav_url=dav.url, storage_webdav_user="u", storage_webdav_password="p")
    add_camera(env, env.alice)
    dav.stop()
    response = page(env, "alice", "correct horse")
    text = response.get_data(as_text=True)
    assert response.status_code == 200 and "could not load" in text.lower() and "Traceback" not in text


def test_cards_use_only_own_camera_media_urls(tmp_path):
    env = make(tmp_path, storage="local", storage_local_root=str(tmp_path / "videos"))
    mine, theirs = add_camera(env, env.bob, "Bob cam"), add_camera(env, env.alice, "Alice cam")
    put_video(env, env.bob, mine, "bob clip")
    put_video(env, env.alice, theirs, "alice clip")
    text = page(env, "bob", "another pass").get_data(as_text=True)
    assert f"/media/{mine}/" in text and f"/media/{theirs}/" not in text and "alice clip" not in text


def test_hostile_video_ids_and_names_are_escaped(tmp_path):
    env = make(tmp_path)
    add_camera(env, env.alice)
    link(env, env.alice, "token-alice")
    bad = 'x"><script>alert(1)</script>'
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: FakeYoutube(
        [{"items": [playlist("P", f"Tapo {DAY}")]}], {"P": [item(bad, "t", privacy="unlisted")]})
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert "<script>alert(1)" not in text and 'data-ref="x&#34;&gt;&lt;script&gt;' in text


def test_notice_about_an_ignored_setting_is_shown_with_a_link(tmp_path):
    import json
    env = make(tmp_path, allow_own_storage=True, storage_allowed=("youtube",))
    env.db.save_storage_config(env.alice, "webdav", env.vault.encrypt(json.dumps({"url": "https://x.example/dav"})))
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert "no longer allowed" in text and 'href="/storage"' in text


def test_private_youtube_video_has_no_watch_here_but_keeps_the_link_and_a_note(tmp_path):
    env = make(tmp_path)
    add_camera(env, env.alice)
    link(env, env.alice, "token-alice")
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: FakeYoutube(
        [{"items": [playlist("P", f"Tapo {DAY}")]}], {"P": [item("secret1", "clip", privacy="private")]})
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert "Watch here" not in text and "data-watch" not in text
    assert len(buttons(text)) == 1 and "watch?v=secret1" in buttons(text)[0]
    assert "Private videos" in text and "open them on YouTube" in text


def test_note_about_private_videos_is_absent_when_every_video_can_be_watched(tmp_path):
    env = make(tmp_path)
    add_camera(env, env.alice)
    link(env, env.alice, "token-alice")
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: FakeYoutube(
        [{"items": [playlist("P", f"Tapo {DAY}")]}], {"P": [item("pub1", "clip", privacy="public")]})
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert "Watch here" in text and "Private videos" not in text


def test_file_videos_are_always_watchable(tmp_path):
    env = make(tmp_path, storage="local", storage_local_root=str(tmp_path / "videos"))
    cid = add_camera(env, env.alice)
    put_video(env, env.alice, cid)
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert "Watch here" in text and "Private videos" not in text
