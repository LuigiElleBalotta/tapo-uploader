import base64
import dataclasses
import hashlib
import re
import threading
import time as clock
from datetime import datetime, time


def _ctx(tmp_path, **overrides):
    import camera_context
    values = dict(name="cam", host="192.0.2.10", user="u", password="p@ss:w!rd", data_dir=tmp_path)
    values.update(overrides)
    return camera_context.CameraContext(**values)


def _reply(name, value):
    return ("<tt:NotificationMessage><tt:Message><tt:Data>"
            f'<tt:SimpleItem Value="{value}" Name="{name}"/>'
            "</tt:Data></tt:Message></tt:NotificationMessage>")


def test_soap_digest_uses_ctx_password(single_user_env, tmp_path):
    ctx = _ctx(tmp_path)
    xml = single_user_env.motion.soap_envelope(ctx, "<Body/>").decode()
    nonce = base64.b64decode(re.search(r"<wsse:Nonce[^>]*>([^<]+)<", xml)[1])
    created = re.search(r"<wsu:Created>([^<]+)<", xml)[1]
    digest = re.search(r"<wsse:Password[^>]*>([^<]+)<", xml)[1]
    expected = base64.b64encode(hashlib.sha1(nonce + created.encode() + ctx.password.encode()).digest()).decode()
    assert digest == expected
    assert ctx.password not in xml
    assert "<wsse:Username>u</wsse:Username>" in xml


def test_two_contexts_do_not_share_buffer(single_user_env, tmp_path):
    a, b = _ctx(tmp_path / "a"), _ctx(tmp_path / "b")
    a.buffer_dir.mkdir(parents=True)
    (a.buffer_dir / "20261026_100000.mp4").write_bytes(b"")
    assert len(single_user_env.motion.segments(a)) == 1
    assert single_user_env.motion.segments(b) == []


def test_motion_states_respects_ctx_events(single_user_env, tmp_path):
    ctx = _ctx(tmp_path, motion_events=frozenset({"ispeople"}))
    motion_states = single_user_env.motion.motion_states
    assert list(motion_states(ctx, _reply("IsMotion", "true"))) == []
    assert list(motion_states(ctx, _reply("IsPeople", "true"))) == [("IsPeople", True)]


def test_ensure_dirs_creates_layout(single_user_env, tmp_path):
    ctx = _ctx(tmp_path / "data")
    ctx.ensure_dirs()
    assert ctx.record_dir.is_dir() and ctx.buffer_dir.is_dir() and ctx.events_dir.is_dir()


def test_worker_reports_error_without_crashing(single_user_env, tmp_path, monkeypatch):
    import camera_worker
    motion, uploader = single_user_env.motion, single_user_env.uploader

    # no ffmpeg and no camera in the test: the three long-running loops just wait for the stop signal
    monkeypatch.setattr(uploader, "record_loop", lambda ctx, out_dir, seconds, stop, **k: stop.wait())
    monkeypatch.setattr(motion, "onvif_loop", lambda ctx, events, stop, report=None: stop.wait())
    monkeypatch.setattr(motion, "controller", lambda ctx, events, stop, report=None: stop.wait())

    def broken():
        raise RuntimeError("token revoked")

    ctx = dataclasses.replace(_ctx(tmp_path), youtube=broken)
    ctx.ensure_dirs()
    day = ctx.events_dir / "20200101"
    day.mkdir(parents=True)
    (day / "120000.mp4").write_bytes(b"x")  # a day that is due: the upload will ask for the broken client
    seen = []
    worker = camera_worker.CameraWorker(ctx, lambda kind, detail: seen.append((kind, detail)))
    worker.start()
    deadline = clock.time() + 10
    while not any(kind == "error" for kind, _ in seen) and clock.time() < deadline:
        clock.sleep(0.1)
    assert any(kind == "error" and "token revoked" in detail for kind, detail in seen)
    assert worker.alive
    worker.stop()
    assert not worker.alive


def test_soap_error_does_not_leak_credentials(single_user_env, tmp_path, monkeypatch):
    motion = single_user_env.motion
    ctx = _ctx(tmp_path, password="p@ss:w!rd")
    reply = f"HTTP/1.1 500 Error\r\n\r\nfailed to open {ctx.rtsp_url()}</s:Envelope>".encode()

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def sendall(self, data):
            pass

        def recv(self, size):
            return reply

    monkeypatch.setattr(motion.socket, "create_connection", lambda *a, **k: FakeSocket())
    try:
        motion.soap(ctx, "http://192.0.2.10:2020/onvif/device_service", "<Body/>")
    except RuntimeError as err:
        message = str(err)
    else:
        raise AssertionError("expected RuntimeError")
    assert "p%40ss%3Aw%21rd" not in message and "p@ss:w!rd" not in message
    assert "***" in message
