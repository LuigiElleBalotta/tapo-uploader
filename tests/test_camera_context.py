from datetime import time
from pathlib import Path

import pytest


def _ctx_module():
    import camera_context
    return camera_context


def test_from_env_matches_module_values(single_user_env):
    ctx = _ctx_module().CameraContext.from_env()
    assert (ctx.host, ctx.port, ctx.user, ctx.password, ctx.stream) == (
        "192.168.1.37", 554, "cam", "p@ss:w!rd", "stream1")
    assert ctx.data_dir == single_user_env.data
    assert (ctx.pre, ctx.post, ctx.extend_within, ctx.segment) == (30, 300, 30, 10)
    assert ctx.segment_seconds == 21600
    assert ctx.upload_at == time(21, 0)
    assert ctx.motion_events == frozenset({"ismotion", "ispeople"})
    assert (ctx.onvif_host, ctx.onvif_port) == ("192.168.1.37", 2020)
    assert (ctx.playlist_prefix, ctx.privacy) == ("Tapo", "private")


@pytest.mark.parametrize("password,quoted", [("a b", "a%20b"), ("x:y@z", "x%3Ay%40z"),
                                             ("100%", "100%25"), ("!", "%21")])
def test_rtsp_url_special_characters(single_user_env, password, quoted):
    ctx = _ctx_module().CameraContext(name="c", host="cam.example.com", port=38554, user="u", password=password)
    assert ctx.rtsp_url() == f"rtsp://u:{quoted}@cam.example.com:38554/stream1"


def test_scrub_hides_password_everywhere(single_user_env):
    ctx = _ctx_module().CameraContext(name="c", host="h", user="us er", password="p@ss:w!rd")
    secret = "us%20er:p%40ss%3Aw%21rd"
    assert secret in ctx.rtsp_url()
    text = f"error opening {ctx.rtsp_url()} and again {secret}"
    assert secret not in ctx.scrub(text)
    assert "***" in ctx.scrub(text)


def test_state_file_per_data_dir(single_user_env, tmp_path):
    module = _ctx_module()
    a = module.CameraContext(name="a", host="h", data_dir=tmp_path / "a")
    b = module.CameraContext(name="b", host="h", data_dir=tmp_path / "b")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert a.state_file != b.state_file
    uploader = single_user_env.uploader
    uploader.save_state(a, {"playlists": {"2026-10-26": "PL-A"}})
    assert uploader.load_state(a) == {"playlists": {"2026-10-26": "PL-A"}}
    assert uploader.load_state(b) == {"playlists": {}}
