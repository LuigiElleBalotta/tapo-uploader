import threading
from datetime import datetime, time, timedelta, timezone

import pytest
from googleapiclient.errors import HttpError

from tapo_web.quota import (DAY_UPLOAD_COST, LIST_COST, PLAYLIST_INSERT_COST, PLAYLIST_ITEM_COST, UPLOAD_COST,
                            QuotaLedger, UploadGate, pacific_day, stagger_slots)
from tapo_web.youtube_link import LinkRevoked
from tests.conftest import Env


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_costs_match_the_spec():
    assert (UPLOAD_COST, PLAYLIST_ITEM_COST, PLAYLIST_INSERT_COST, LIST_COST) == (1600, 50, 50, 1)
    assert DAY_UPLOAD_COST == 1701


def test_pacific_day_boundary():
    # midnight Pacific is 07:00 UTC while daylight saving time lasts (until 2026-11-01)
    assert pacific_day(utc(2026, 10, 27, 6, 59)) == "2026-10-26"
    assert pacific_day(utc(2026, 10, 27, 7, 0)) == "2026-10-27"


def test_dst_change_day():
    # after the clocks go back (2026-11-01) midnight Pacific is 08:00 UTC
    assert pacific_day(utc(2026, 11, 2, 7, 59)) == "2026-11-01"
    assert pacific_day(utc(2026, 11, 2, 8, 0)) == "2026-11-02"


def test_ledger_counts_per_pacific_day(tmp_path):
    env = Env(tmp_path)
    ledger = QuotaLedger(env.db, 10000)
    ledger.record(1600, utc(2026, 10, 27, 6, 59))
    ledger.record(50, utc(2026, 10, 27, 7, 1))
    assert ledger.used(utc(2026, 10, 27, 6, 30)) == 1600
    assert ledger.used(utc(2026, 10, 27, 12, 0)) == 50


def test_can_afford_stops_at_limit(tmp_path):
    env = Env(tmp_path)
    ledger = QuotaLedger(env.db, 10000)
    moment = utc(2026, 10, 26, 20, 0)
    for _ in range(5):
        ledger.record(DAY_UPLOAD_COST, moment)
    assert ledger.used(moment) == 8505
    assert not ledger.can_afford(DAY_UPLOAD_COST, moment)  # a sixth full upload would need 10206
    assert ledger.can_afford(1000, moment)
    fresh = QuotaLedger(Env(tmp_path / "other").db, 10000)
    for _ in range(4):
        fresh.record(DAY_UPLOAD_COST, moment)
    assert fresh.can_afford(DAY_UPLOAD_COST, moment)  # the fifth user still fits


def test_stagger_slots():
    assert stagger_slots([7, 3, 9], time(21, 0), 10) == {3: time(21, 0), 7: time(21, 10), 9: time(21, 20)}


def test_stagger_wraps_within_the_day():
    assert stagger_slots([1, 2, 3], time(23, 50), 10) == {1: time(23, 50), 2: time(0, 0), 3: time(0, 10)}


def test_gate_waits_for_slot_and_quota(tmp_path):
    env = Env(tmp_path)
    ledger = QuotaLedger(env.db, 2000)
    gate = UploadGate(ledger, 1, time(21, 10))
    day = datetime(2026, 10, 26)
    assert not gate.allowed(day.replace(hour=21, minute=9))
    assert gate.allowed(day.replace(hour=21, minute=10))
    gate.charge(day.replace(hour=21, minute=10))
    assert not gate.allowed(day.replace(hour=21, minute=30))  # 1701 used, another day's upload does not fit in 2000


def test_gate_exhausted_blocks_everyone_for_the_day(tmp_path):
    env = Env(tmp_path)
    ledger = QuotaLedger(env.db, 10000)
    first, second = UploadGate(ledger, 1, time(21, 0)), UploadGate(ledger, 2, time(21, 10))
    moment = datetime(2026, 10, 26, 22, 0)
    assert first.allowed(moment) and second.allowed(moment)
    first.exhausted(moment)  # Google itself said quotaExceeded
    assert not first.allowed(moment) and not second.allowed(moment)
    assert second.allowed(moment + timedelta(days=1))  # a new Pacific day starts fresh


# ----- the daily loop under a gate and under failures -----

class FailingRequest:
    def __init__(self, error):
        self.error = error

    def next_chunk(self):
        raise self.error


class FailingYoutube:
    def __init__(self, error):
        self.error = error

    def videos(self):
        return self

    def insert(self, **kwargs):
        return FailingRequest(self.error)


def one_tick(stop_after_wait=True):
    stop = threading.Event()
    waits = []
    stop.wait = lambda seconds=None: (waits.append(seconds), stop.set())[0]
    return stop, waits


def due_clip(ctx):
    ctx.ensure_dirs()
    day = ctx.events_dir / "20200101"
    day.mkdir(parents=True)
    clip = day / "120000.mp4"
    clip.write_bytes(b"x")
    return day, clip


def test_revoked_token_pauses_uploads_and_keeps_clips(single_user_env, tmp_path):
    import camera_context
    motion = single_user_env.motion
    calls = []

    def revoked():
        calls.append(1)
        raise LinkRevoked("Google no longer accepts the saved token")

    ctx = camera_context.CameraContext(name="c", host="h", data_dir=tmp_path, youtube=revoked)
    day, clip = due_clip(ctx)
    seen = []
    for tick in range(2):  # the next tick retries without raising
        stop, _ = one_tick()
        motion.daily_loop(ctx, stop, lambda kind, detail="": seen.append((kind, detail)))
    assert len(calls) == 2 and clip.exists()
    assert [kind for kind, _ in seen] == ["error", "error"] and "no longer accepts" in seen[0][1]


def test_quota_error_keeps_clips_and_marks_the_day_exhausted(single_user_env, tmp_path, monkeypatch):
    import camera_context
    motion = single_user_env.motion
    monkeypatch.setattr(motion, "join", lambda parts, out: out.write_bytes(b"joined"))
    monkeypatch.setattr(motion, "clip_seconds", lambda path: 10.0)
    from types import SimpleNamespace
    error = HttpError(SimpleNamespace(status=403, reason="quota"), b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}')
    ctx = camera_context.CameraContext(name="c", host="h", data_dir=tmp_path, youtube=lambda: FailingYoutube(error))
    day, clip = due_clip(ctx)
    env = Env(tmp_path / "db")
    ledger = QuotaLedger(env.db, 10000)
    gate = UploadGate(ledger, 1, time(0, 0))
    seen = []
    stop, waits = one_tick()
    motion.daily_loop(ctx, stop, lambda kind, detail="": seen.append((kind, detail)), gate)
    assert clip.exists() and waits == [3600]
    assert ("error", "YouTube quota exceeded") in seen
    assert not gate.allowed(datetime.now())  # the other cameras stop trying for today too


def test_gate_that_is_closed_does_not_touch_youtube(single_user_env, tmp_path):
    import camera_context
    motion = single_user_env.motion
    calls = []
    ctx = camera_context.CameraContext(name="c", host="h", data_dir=tmp_path, youtube=lambda: calls.append(1))
    due_clip(ctx)
    env = Env(tmp_path / "db")
    closed = UploadGate(QuotaLedger(env.db, 10), 1, time(0, 0))  # 10 units: nothing fits
    motion.upload_pending(ctx, {"playlists": {}}, gate=closed)
    assert calls == []
