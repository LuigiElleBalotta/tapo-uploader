"""rclone backend with a fake runner: every call is a plain argument list, never a shell string."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tapo_storage.base import StorageError, make_storage
from tapo_storage.rclone import RcloneStorage


class FakeRun:
    """Records argument lists; `replies` maps the rclone subcommand to (returncode, stdout, stderr)."""

    def __init__(self, replies=None, missing=False):
        self.calls, self.replies, self.missing = [], replies or {}, missing

    def __call__(self, cmd, **kwargs):
        if self.missing:
            raise FileNotFoundError("rclone")
        assert isinstance(cmd, list) and not kwargs.get("shell")
        self.calls.append(cmd)
        sub = next(arg for arg in cmd[1:] if not arg.startswith("-") and arg != "cfg.conf")
        code, out, err = self.replies.get(sub, (0, "", ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    def subs(self):
        return [next(a for a in c[1:] if not a.startswith("-") and a != "cfg.conf") for c in self.calls]


def lsjson(*entries):
    return (0, json.dumps([{"Name": n, "Size": s, "IsDir": d, "ModTime": "2026-10-26T12:30:00Z"} for n, s, d in entries]), "")


def source(tmp_path):
    path = tmp_path / "day.mp4"
    path.write_bytes(b"0123456789")
    return path


def test_check_reports_missing_binary():
    with pytest.raises(StorageError, match="not installed"):
        RcloneStorage("mega", None, "Tapo", run=FakeRun(missing=True)).check()


def test_check_reports_bad_remote():
    run = FakeRun({"lsd": (1, "", "Failed to create file system for \"mega:\": didn't find section in config file\nmore")})
    with pytest.raises(StorageError, match="remote 'mega' is not usable: Failed to create file system"):
        RcloneStorage("mega", None, "Tapo", run=run).check()


def test_check_passes_when_the_remote_lists():
    run = FakeRun()
    RcloneStorage("mega", None, "Tapo", run=run).check()
    assert run.calls == [["rclone", "lsd", "mega:"]]


def test_put_copies_to_part_then_moves_and_never_uses_a_shell(tmp_path):
    run = FakeRun()
    item = RcloneStorage("mega", None, "Tapo", folder="cams", run=run).put(source(tmp_path), "2026-10-26", "v (1)", "00:00 x")
    assert run.subs() == ["copyto", "moveto", "copyto"]
    assert run.calls[0][-1] == "mega:cams/Tapo 2026-10-26/v (1).mp4.part"
    assert run.calls[1][-2:] == ["mega:cams/Tapo 2026-10-26/v (1).mp4.part", "mega:cams/Tapo 2026-10-26/v (1).mp4"]
    assert run.calls[2][-1] == "mega:cams/Tapo 2026-10-26/v (1).chapters.txt"
    assert item.name == "v (1).mp4" and item.size == 10 and item.url is None


def test_put_failure_keeps_no_final_name(tmp_path):
    run = FakeRun({"copyto": (1, "", "quota exceeded")})
    with pytest.raises(StorageError, match="quota exceeded"):
        RcloneStorage("mega", None, "Tapo", run=run).put(source(tmp_path), "2026-10-26", "v", "")
    assert "moveto" not in run.subs()


def test_names_with_spaces_hash_and_unicode_are_passed_as_single_arguments(tmp_path):
    run = FakeRun()
    RcloneStorage("mega", None, "Tapo", run=run).put(source(tmp_path), "2026-10-26", "Cámara #1 ? x", "")
    destination = run.calls[0][-1]
    assert destination.startswith("mega:Tapo 2026-10-26/") and "#" not in destination and "?" not in destination
    assert " " in destination and "á" in destination


def test_list_day_parses_lsjson_and_skips_part_files():
    run = FakeRun({"lsjson": lsjson(("a.mp4", 5, False), ("b.mp4.part", 1, False), ("a.chapters.txt", 3, False), ("sub", 0, True))})
    items = RcloneStorage("mega", None, "Tapo", run=run).list_day("2026-10-26")
    assert [(i.name, i.size, i.modified, i.url) for i in items] == [("a.mp4", 5, "2026-10-26 12:30", None)]
    assert run.calls[0][-1] == "mega:Tapo 2026-10-26"


def test_days_newest_first_and_missing_folder_is_empty():
    run = FakeRun({"lsjson": lsjson(("Tapo 2026-10-24", 0, True), ("Tapo 2026-10-26", 0, True), ("other", 0, True), ("Tapo 2026-10-25", 0, False))})
    assert RcloneStorage("mega", None, "Tapo", run=run).days() == ["2026-10-26", "2026-10-24"]
    gone = FakeRun({"lsjson": (3, "", "directory not found")})
    assert RcloneStorage("mega", None, "Tapo", run=gone).days() == []


def test_open_uses_offset_and_count():
    run = FakeRun({"lsjson": lsjson(("a.mp4", 10, False))})
    launched = []

    class Proc:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)
            self.stdout = SimpleNamespace(read=lambda n=None, it=iter([b"2345", b""]): next(it), close=lambda: None)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    chunks, total = RcloneStorage("mega", None, "Tapo", run=run, popen=Proc).open("2026-10-26", "a.mp4", start=2, end=5)
    assert b"".join(chunks) == b"2345" and total == 10
    assert launched[0][1:6] == ["cat", "--offset", "2", "--count", "4"] and launched[0][-1] == "mega:Tapo 2026-10-26/a.mp4"


def test_open_unknown_file_is_refused():
    run = FakeRun({"lsjson": lsjson(("a.mp4", 10, False))})
    for bad in ("../a.mp4", "x.mp4", "a/b.mp4"):
        with pytest.raises(StorageError):
            RcloneStorage("mega", None, "Tapo", run=run, popen=None).open("2026-10-26", bad)


def test_config_file_is_passed_with_dash_dash_config():
    run = FakeRun()
    RcloneStorage("mega", Path("cfg.conf"), "Tapo", run=run).check()
    assert run.calls[0][:3] == ["rclone", "--config", "cfg.conf"]


def test_remote_and_folder_are_validated():
    for remote in ("", "me ga", "a:b", "x;rm", "-evil"):
        with pytest.raises(StorageError):
            RcloneStorage(remote, None, "Tapo")
    with pytest.raises(StorageError):
        RcloneStorage("mega", None, "Tapo", folder="a/../b")


def test_make_storage_builds_rclone():
    s = make_storage("rclone", {"remote": "mega", "config": "/etc/rclone.conf", "folder": "cams"}, None)
    assert isinstance(s, RcloneStorage) and s.kind == "rclone"
