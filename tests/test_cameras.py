import socket
import subprocess

import pytest

from tapo_web.cameras import ProbeResult, context_for, probe_camera, safe_context
from tapo_web.security import UnsafeHost
from tests.conftest import Env

FORM = {"name": "Living room", "host": "my-cam.example.com", "camera_user": "camuser",
        "camera_password": "S3cretPW", "stream": "stream1"}


@pytest.fixture
def two(tmp_path):
    env = Env(tmp_path, max_cameras_per_user=2)
    env.alice = env.add_local_user("alice", "correct horse")
    env.bob = env.add_local_user("bob", "another pass")
    return env


def create(env, client=None, **overrides):
    client = client or env.client
    return client.post("/cameras/new", data={"csrf_token": env.csrf(client, "/cameras/new"), **FORM, **overrides})


def as_user(env, username, password):
    client = env.app.test_client()
    env.login(username, password, client=client)
    return client


def test_create_camera_assigns_ports_and_stores_encrypted(two):
    alice = as_user(two, "alice", "correct horse")
    response = create(two, alice)
    assert response.status_code == 302
    camera = two.db.cameras_for_user(two.alice)[0]
    assert response.headers["Location"] == f"/cameras/{camera['id']}/guide"
    low, high = two.settings.port_range
    assert low <= camera["port"] <= high and low <= camera["onvif_port"] <= high
    assert abs(camera["port"] - camera["onvif_port"]) > 1 and camera["enabled"] == 0
    assert "camuser" not in camera["username_enc"] and "S3cretPW" not in camera["password_enc"]
    assert two.vault.decrypt(camera["password_enc"]) == "S3cretPW"


def test_camera_limit_per_user(tmp_path):
    env = Env(tmp_path)  # default: one camera per user
    env.add_local_user("alice", "correct horse")
    alice = as_user(env, "alice", "correct horse")
    assert create(env, alice).status_code == 302
    assert create(env, alice).status_code == 400
    assert len(env.db.cameras_for_user(1)) == 1


def test_total_camera_limit(tmp_path):
    env = Env(tmp_path, max_cameras_total=1)
    env.add_local_user("alice", "correct horse")
    env.add_local_user("bob", "another pass")
    assert create(env, as_user(env, "alice", "correct horse")).status_code == 302
    assert create(env, as_user(env, "bob", "another pass")).status_code == 400


def test_user_cannot_open_other_users_camera_pages(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    cam_id = two.db.cameras_for_user(two.alice)[0]["id"]
    bob = as_user(two, "bob", "another pass")
    token = two.csrf(bob, "/cameras/new")
    assert bob.get(f"/cameras/{cam_id}").status_code == 404
    assert bob.get(f"/cameras/{cam_id}/guide").status_code == 404
    for action in ("", "/test", "/delete"):
        assert bob.post(f"/cameras/{cam_id}{action}", data={"csrf_token": token, **FORM}).status_code == 404
    assert two.db.camera_for_user(two.alice, cam_id) is not None  # nothing of alice's changed


def test_unsafe_host_refused_with_message(two):
    alice = as_user(two, "alice", "correct horse")
    for host in ("192.168.1.5", "127.0.0.1", "169.254.169.254"):
        response = create(two, alice, host=host)
        assert response.status_code == 400 and "not allowed" in response.get_data(as_text=True)
    assert two.db.cameras_for_user(two.alice) == []


def test_private_host_allowed_with_the_flag(tmp_path):
    env = Env(tmp_path, allow_private_hosts=True)
    env.add_local_user("alice", "correct horse")
    assert create(env, as_user(env, "alice", "correct horse"), host="192.168.1.5").status_code == 302


def test_invalid_input_is_refused(two):
    alice = as_user(two, "alice", "correct horse")
    for bad in ({"name": ""}, {"name": "x" * 61}, {"stream": "stream9"}, {"camera_user": ""},
                {"camera_password": ""}, {"host": "bad host"}):
        assert create(two, alice, **bad).status_code == 400, bad


def test_password_never_rendered(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    cam_id = two.db.cameras_for_user(two.alice)[0]["id"]
    two.app.extensions["tapo"]["probe"] = lambda ctx: ProbeResult(True, True, True, "ok")
    pages = [alice.get(f"/cameras/{cam_id}"), alice.get(f"/cameras/{cam_id}/guide"),
             alice.post(f"/cameras/{cam_id}/test", data={"csrf_token": two.csrf(alice, "/cameras/new")})]
    for page in pages:
        text = page.get_data(as_text=True)
        assert page.status_code == 200 and "S3cretPW" not in text and "camuser" not in text


def test_update_keeps_password_when_blank(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    cam_id = two.db.cameras_for_user(two.alice)[0]["id"]
    token = two.csrf(alice, "/cameras/new")
    response = alice.post(f"/cameras/{cam_id}", data={"csrf_token": token, "name": "Kitchen", "host": "my-cam.example.com",
                                                      "camera_user": "camuser", "camera_password": "", "stream": "stream2"})
    assert response.status_code == 302
    camera = two.db.camera_for_user(two.alice, cam_id)
    assert camera["name"] == "Kitchen" and camera["stream"] == "stream2"
    assert two.vault.decrypt(camera["password_enc"]) == "S3cretPW"


def test_delete_camera(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    cam_id = two.db.cameras_for_user(two.alice)[0]["id"]
    alice.post(f"/cameras/{cam_id}/delete", data={"csrf_token": two.csrf(alice, "/cameras/new")})
    assert two.db.cameras_for_user(two.alice) == []


def test_guide_page_shows_ports_and_server_ip(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    camera = two.db.cameras_for_user(two.alice)[0]
    text = alice.get(f"/cameras/{camera['id']}/guide").get_data(as_text=True)
    for needed in (str(camera["port"]), str(camera["onvif_port"]), "203.0.113.7"):
        assert needed in text


# ----- connection test -----

def test_successful_test_enables_the_camera(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    cam_id = two.db.cameras_for_user(two.alice)[0]["id"]
    two.app.extensions["tapo"]["probe"] = lambda ctx: ProbeResult(True, True, True, "all good")
    alice.post(f"/cameras/{cam_id}/test", data={"csrf_token": two.csrf(alice, "/cameras/new")})
    camera = two.db.camera_for_user(two.alice, cam_id)
    assert camera["enabled"] == 1 and camera["status"] == "ready" and camera["last_error"] is None


def test_failed_test_keeps_the_camera_disabled(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    cam_id = two.db.cameras_for_user(two.alice)[0]["id"]
    two.app.extensions["tapo"]["probe"] = lambda ctx: ProbeResult(True, False, False, "ffprobe failed")
    response = alice.post(f"/cameras/{cam_id}/test", data={"csrf_token": two.csrf(alice, "/cameras/new")})
    camera = two.db.camera_for_user(two.alice, cam_id)
    assert camera["enabled"] == 0 and "ffprobe failed" in camera["last_error"]
    assert "ffprobe failed" in response.get_data(as_text=True)


def test_context_for_decrypts_and_isolates_data(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    camera = two.db.cameras_for_user(two.alice)[0]
    ctx = context_for(camera, two.vault, two.settings)
    assert (ctx.user, ctx.password, ctx.host, ctx.port, ctx.onvif_port) == (
        "camuser", "S3cretPW", "my-cam.example.com", camera["port"], camera["onvif_port"])
    assert two.settings.data_dir in ctx.data_dir.parents
    assert str(camera["id"]) in ctx.data_dir.name


def test_safe_context_connects_to_the_checked_ip(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    camera = two.db.cameras_for_user(two.alice)[0]
    ctx = safe_context(camera, two.vault, two.settings, resolver=two.app.extensions["tapo"]["resolver"])
    assert ctx.host == "93.184.216.34" and ctx.onvif_host == "93.184.216.34"


def test_safe_context_refuses_a_host_that_became_private(two):
    alice = as_user(two, "alice", "correct horse")
    create(two, alice)
    camera = two.db.cameras_for_user(two.alice)[0]

    def rebinding(host, port=None, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    with pytest.raises(UnsafeHost):
        safe_context(camera, two.vault, two.settings, resolver=rebinding)


def make_ctx(**kw):
    from camera_context import CameraContext
    return CameraContext(name="c", host="93.184.216.34", user="u", password="p@ss", **kw)


class Connect:
    def __init__(self, ok):
        self.ok = ok

    def __call__(self, address, timeout=None):
        if not self.ok:
            raise OSError("refused")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_probe_reports_each_stage():
    ctx = make_ctx()
    ffprobe_fails = lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", f"error opening {ctx.rtsp_url()}")
    onvif_ok = lambda c: "http://pull"
    result = probe_camera(ctx, runner=ffprobe_fails, connect=Connect(True), onvif=onvif_ok)
    assert (result.tcp, result.rtsp, result.onvif) == (True, False, True)
    assert "p@ss" not in result.detail and "p%40ss" not in result.detail
    closed = probe_camera(ctx, runner=ffprobe_fails, connect=Connect(False), onvif=onvif_ok)
    assert (closed.tcp, closed.rtsp) == (False, False)  # no point probing RTSP on a closed port


def test_probe_all_ok():
    ctx = make_ctx()
    good = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "codec_name=h264\n", "")
    result = probe_camera(ctx, runner=good, connect=Connect(True), onvif=lambda c: "http://pull")
    assert (result.tcp, result.rtsp, result.onvif) == (True, True, True)


def test_probe_onvif_failure_is_reported():
    ctx = make_ctx()
    good = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "codec_name=h264\n", "")

    def broken(c):
        raise RuntimeError("ONVIF error: 401")

    result = probe_camera(ctx, runner=good, connect=Connect(True), onvif=broken)
    assert result.rtsp is True and result.onvif is False and "ONVIF" in result.detail
