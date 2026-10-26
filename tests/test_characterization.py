"""Pins the single-user behavior as it was before the multi-account refactor."""
from datetime import datetime, time


def _ctx():
    import camera_context
    return camera_context.CameraContext.from_env()


def test_rtsp_url_quotes_credentials(single_user_env):
    url = _ctx().rtsp_url()
    assert url == "rtsp://cam:p%40ss%3Aw%21rd@192.168.1.37:554/stream1"


def test_scrub_hides_password(single_user_env):
    ctx = _ctx()
    assert ctx.scrub(ctx.rtsp_url()) == "rtsp://***@192.168.1.37:554/stream1"


def test_ffmpeg_cmd_segments(single_user_env, tmp_path):
    out_dir = tmp_path / "rec"
    cmd = single_user_env.uploader.ffmpeg_cmd(_ctx(), out_dir, 21600)
    assert cmd[:4] == ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    joined = " ".join(cmd)
    assert "-c:v copy" in joined
    assert "-segment_atclocktime 1" in joined
    assert "movflags=+frag_keyframe+empty_moov+default_base_moof" in joined
    assert cmd[-1] == str(out_dir / "%Y%m%d_%H%M%S.mp4")


def _reply(name, value):
    return ("<tt:NotificationMessage><tt:Message><tt:Data>"
            f'<tt:SimpleItem Value="{value}" Name="{name}"/>'
            "</tt:Data></tt:Message></tt:NotificationMessage>")


def test_motion_states_value_before_name(single_user_env):
    motion_states, ctx = single_user_env.motion.motion_states, _ctx()
    assert list(motion_states(ctx, _reply("IsMotion", "true"))) == [("IsMotion", True)]
    assert list(motion_states(ctx, _reply("IsPeople", "false"))) == [("IsPeople", False)]
    assert list(motion_states(ctx, _reply("Other", "true"))) == []


def test_on_motion_extends_window(single_user_env):
    motion, ctx = single_user_env.motion, _ctx()
    assert motion.on_motion(ctx, None, 100.0) == motion.Event(100.0, 400.0)
    assert motion.on_motion(ctx, motion.Event(100.0, 400.0), 380.0).end == 680.0
    assert motion.on_motion(ctx, motion.Event(100.0, 400.0), 200.0).end == 400.0


def test_timestamp(single_user_env):
    assert single_user_env.motion.timestamp(65) == "1:05"
    assert single_user_env.motion.timestamp(3725) == "1:02:05"


def test_segments_sorted(single_user_env):
    motion, ctx = single_user_env.motion, _ctx()
    ctx.buffer_dir.mkdir(parents=True, exist_ok=True)
    (ctx.buffer_dir / "20261026_100000.mp4").write_bytes(b"")
    (ctx.buffer_dir / "20261026_095950.mp4").write_bytes(b"")
    (ctx.buffer_dir / "notes.txt").write_bytes(b"")
    assert [path.name for _, path in motion.segments(ctx)] == ["20261026_095950.mp4", "20261026_100000.mp4"]


def _fixed_now(motion, monkeypatch, moment):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(motion, "datetime", Fixed)


def test_due_days(single_user_env, monkeypatch):
    motion, ctx = single_user_env.motion, _ctx()
    ctx.events_dir.mkdir(parents=True, exist_ok=True)
    (ctx.events_dir / "20261025").mkdir()
    (ctx.events_dir / "20261026").mkdir()
    assert ctx.upload_at == time(21, 0)
    _fixed_now(motion, monkeypatch, datetime(2026, 10, 26, 12, 0, 0))
    assert [p.name for p in motion.due_days(ctx)] == ["20261025"]
    assert [p.name for p in motion.due_days(ctx, force=True)] == ["20261025", "20261026"]
    _fixed_now(motion, monkeypatch, datetime(2026, 10, 26, 21, 30, 0))
    assert [p.name for p in motion.due_days(ctx)] == ["20261025", "20261026"]
