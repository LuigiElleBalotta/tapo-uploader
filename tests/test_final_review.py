"""Findings of the final whole-branch review, each pinned by a test that failed before its fix."""
import pytest

from tapo_web.config import Settings
from tests.conftest import Env, make_settings

FORM = {"name": "Living room", "host": "my-cam.example.com", "camera_user": "camuser",
        "camera_password": "S3cretPW", "stream": "stream1"}


# 1. open redirect after login --------------------------------------------------

@pytest.mark.parametrize("target", ["/\\evil.example.com", "/\t/evil.example.com", "\\\\evil.example.com",
                                    "/\\/evil.example.com", "/%5Cevil.example.com", "/ok\r\nSet-Cookie: x=1"])
def test_open_redirect_blocked_for_backslash_and_control_characters(web, target):
    web.add_local_user("alice", "correct horse")
    response = web.login("alice", "correct horse", next=target)
    assert response.status_code == 302 and response.headers["Location"] == "/"


def test_local_paths_still_work_after_login(web):
    web.add_local_user("alice", "correct horse")
    assert web.login("alice", "correct horse", next="/cameras/new?x=1").headers["Location"] == "/cameras/new?x=1"


# 2. login limits behind a reverse proxy -------------------------------------------

def attempt(env, ip, password, forwarded=True):
    headers = {"X-Forwarded-For": ip} if forwarded else {}
    token = env.csrf()
    return env.client.post("/login", data={"username": "alice", "password": password, "csrf_token": token},
                           headers=headers, environ_overrides={"REMOTE_ADDR": "10.0.0.1"})  # the proxy


def test_forwarded_for_is_ignored_unless_a_proxy_is_trusted(web):
    web.add_local_user("alice", "correct horse")
    for _ in range(5):
        attempt(web, "1.1.1.1", "wrong")
    assert attempt(web, "2.2.2.2", "correct horse").status_code == 429  # spoofing the header changes nothing


def test_trusted_proxy_separates_clients(tmp_path):
    env = Env(tmp_path, trusted_proxy_hops=1)
    env.add_local_user("alice", "correct horse")
    for _ in range(5):
        attempt(env, "1.1.1.1", "wrong")
    assert attempt(env, "1.1.1.1", "correct horse").status_code == 429  # the attacker stays blocked
    assert attempt(env, "2.2.2.2", "correct horse").status_code == 302  # the real user is not locked out


def test_trusted_proxy_hops_setting():
    assert Settings.from_env({}).trusted_proxy_hops == 0
    assert Settings.from_env({"TRUSTED_PROXY_HOPS": "1"}).trusted_proxy_hops == 1


# 3. IPv6 camera addresses -----------------------------------------------------------

def test_ipv6_host_is_bracketed_in_urls(single_user_env):
    import camera_context
    ctx = camera_context.CameraContext(name="c", host="2001:db8::1", user="u", password="p", onvif_port=2020)
    assert ctx.rtsp_url() == "rtsp://u:p@[2001:db8::1]:554/stream1"
    assert single_user_env.motion.reach(ctx, "http://192.168.1.2:80/onvif/x") == "http://[2001:db8::1]:2020/onvif/x"


def test_soap_connects_to_a_bracketed_ipv6_url(single_user_env, monkeypatch):
    import camera_context
    motion = single_user_env.motion
    ctx = camera_context.CameraContext(name="c", host="2001:db8::1", user="u", password="p")
    seen = []

    class Boom(Exception):
        pass

    def fake_connect(address, timeout=None):
        seen.append(address)
        raise Boom()

    monkeypatch.setattr(motion.socket, "create_connection", fake_connect)
    with pytest.raises(Boom):
        motion.soap(ctx, "http://[2001:db8::1]:2020/onvif/device_service", "<Body/>")
    assert seen == [("2001:db8::1", 2020)]


def test_ipv4_urls_unchanged(single_user_env):
    import camera_context
    ctx = camera_context.CameraContext(name="c", host="192.168.1.37", user="u", password="p")
    assert ctx.rtsp_url() == "rtsp://u:p@192.168.1.37:554/stream1"
    assert single_user_env.motion.reach(ctx, "http://10.0.0.5:80/x") == "http://192.168.1.37:2020/x"


# 4. deleting a camera deletes its recordings ----------------------------------------------

class Supervisor:
    def __init__(self):
        self.syncs = 0

    def sync(self):
        self.syncs += 1

    def status(self, camera_id):
        return {"running": False, "pending_clips": 0}


def test_deleting_a_camera_removes_its_recordings(tmp_path):
    from tapo_web.cameras import camera_data_dir
    env = Env(tmp_path)
    alice = env.add_local_user("alice", "correct horse")
    env.app.extensions["tapo"]["supervisor"] = supervisor = Supervisor()
    client = env.app.test_client()
    env.login("alice", "correct horse", client=client)
    client.post("/cameras/new", data={"csrf_token": env.csrf(client, "/cameras/new"), **FORM})
    camera = env.db.cameras_for_user(alice)[0]
    folder = camera_data_dir(env.settings, camera)
    clip = folder / "events" / "20261026" / "120000.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"footage")
    client.post(f"/cameras/{camera['id']}/delete", data={"csrf_token": env.csrf(client, "/cameras/new")})
    assert env.db.cameras_for_user(alice) == []
    assert not folder.exists()
    assert supervisor.syncs >= 1  # the recorder was told to stop before the files went
