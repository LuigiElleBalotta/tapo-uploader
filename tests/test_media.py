"""The /media route: recordings streamed to their owner, with Range support."""
import pytest

from tapo_storage.local import LocalStorage
from tests.conftest import Env

DAY = "2026-10-26"


def make(tmp_path, **settings):
    root = tmp_path / "videos"
    env = Env(tmp_path, storage="local", storage_local_root=str(root), **settings)
    alice = env.add_local_user("alice", "correct horse")
    cid = env.db.add_camera(alice, "cam", "cam.example.com", 38554, 38020, "stream1", env.vault.encrypt("u"), env.vault.encrypt("p"))
    source = tmp_path / "src.mp4"
    source.write_bytes(b"0123456789")
    item = LocalStorage(root / str(alice) / f"camera-{cid}", "Tapo").put(source, DAY, "v", "")
    client = env.app.test_client()
    env.login("alice", "correct horse", client=client)
    return env, client, alice, cid, item.name


def url(cid, name, day=DAY):
    return f"/media/{cid}/{day}/{name}"


def test_owner_gets_full_file_200(tmp_path):
    env, client, alice, cid, name = make(tmp_path)
    response = client.get(url(cid, name))
    assert response.status_code == 200 and response.data == b"0123456789"
    assert response.headers["Content-Type"] == "video/mp4" and response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Length"] == "10" and "no-store" in response.headers["Cache-Control"]
    assert "private" in response.headers["Cache-Control"]


def test_range_returns_206_with_content_range(tmp_path):
    env, client, alice, cid, name = make(tmp_path)
    response = client.get(url(cid, name), headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.data == b"2345"
    assert response.headers["Content-Range"] == "bytes 2-5/10" and response.headers["Content-Length"] == "4"


def test_suffix_and_open_ended_ranges(tmp_path):
    env, client, alice, cid, name = make(tmp_path)
    assert client.get(url(cid, name), headers={"Range": "bytes=-3"}).data == b"789"
    open_ended = client.get(url(cid, name), headers={"Range": "bytes=4-"})
    assert open_ended.data == b"456789" and open_ended.headers["Content-Range"] == "bytes 4-9/10"


def test_huge_end_is_clamped_to_the_file(tmp_path):
    env, client, alice, cid, name = make(tmp_path)
    response = client.get(url(cid, name), headers={"Range": "bytes=2-99999999999"})
    assert response.status_code == 206 and response.data == b"23456789"
    assert response.headers["Content-Range"] == "bytes 2-9/10"


@pytest.mark.parametrize("spec", ["bytes=abc", "bytes=5-2", "bytes=20-30", "bytes=0-1,3-4", "bytes=-", "bytes=-0",
                                  "items=0-1", "bytes=" + "9" * 5000 + "-"])
def test_invalid_or_unsatisfiable_range_is_416(tmp_path, spec):
    env, client, alice, cid, name = make(tmp_path)
    response = client.get(url(cid, name), headers={"Range": spec})
    assert response.status_code == 416 and response.headers["Content-Range"] == "bytes */10"


def test_other_users_camera_is_404(tmp_path):
    env, client, alice, cid, name = make(tmp_path)
    env.add_local_user("bob", "another pass")
    bob = env.app.test_client()
    env.login("bob", "another pass", client=bob)
    assert bob.get(url(cid, name)).status_code == 404


def test_admin_can_watch_any_camera(tmp_path):
    env, client, alice, cid, name = make(tmp_path)
    env.add_local_user("root", "admin pass", role="admin")
    admin = env.app.test_client()
    env.login("root", "admin pass", client=admin)
    assert admin.get(url(cid, name)).data == b"0123456789"


@pytest.mark.parametrize("path", ["{cid}/2026-10-26/..%2Fv.mp4", "{cid}/2026-10-26/..\\v.mp4", "{cid}/..%2F../v.mp4",
                                  "{cid}/2026-10-26/v.mp4%00.png", "{cid}/2026-10-26/%2Fetc%2Fpasswd", "{cid}/2026-10-26/other.mp4",
                                  "{cid}/2026-10-26/v.txt", "{cid}/2026-1-26/v.mp4", "999/2026-10-26/v.mp4", "{cid}/2026-10-26/v.mp4.part"])
def test_hostile_or_unknown_names_are_404(tmp_path, path):
    env, client, alice, cid, name = make(tmp_path)
    assert client.get("/media/" + path.format(cid=cid)).status_code == 404


def test_requires_login(tmp_path):
    env, client, alice, cid, name = make(tmp_path)
    anonymous = env.app.test_client()
    assert anonymous.get(url(cid, name)).status_code == 302


def test_youtube_destination_has_no_media_route(tmp_path):
    env = Env(tmp_path)
    alice = env.add_local_user("alice", "correct horse")
    cid = env.db.add_camera(alice, "cam", "cam.example.com", 38554, 38020, "stream1", env.vault.encrypt("u"), env.vault.encrypt("p"))
    client = env.app.test_client()
    env.login("alice", "correct horse", client=client)
    assert client.get(url(cid, "v.mp4")).status_code == 404


def test_broken_destination_is_a_clean_error_not_a_crash(tmp_path):
    env, client, alice, cid, name = make(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "videos")
    assert client.get(url(cid, name)).status_code == 404


def test_csp_allows_youtube_nocookie_frames_and_self_media_only(tmp_path):
    env, client, *_ = make(tmp_path)
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-src https://www.youtube-nocookie.com;" in csp + ";"
    assert "media-src 'self'" in csp and "script-src" not in csp
    assert csp.count("youtube") == 1 and "unsafe" not in csp
