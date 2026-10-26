"""Storage interface, file naming and the local backend."""
from pathlib import Path

import pytest

from tapo_storage import base
from tapo_storage.base import StorageError, StoredItem, make_storage, safe_filename
from tapo_storage.local import LocalStorage


def clip(tmp_path, data=b"0123456789"):
    path = tmp_path / "day.mp4"
    path.write_bytes(data)
    return path


def test_safe_filename_strips_path_and_forbidden_characters():
    assert safe_filename("Tapo 2026-10-26 eventi 12:30-13:10 (3)") == "Tapo 2026-10-26 eventi 12-30-13-10 (3).mp4"
    name = safe_filename("../../x/é#?.mp4")
    assert name.endswith(".mp4") and not any(c in name for c in "/\#?:") and "é" in name
    assert safe_filename("") == "video.mp4"


def test_local_put_writes_file_and_chapters_sidecar(tmp_path):
    storage = LocalStorage(tmp_path / "root", "Tapo")
    item = storage.put(clip(tmp_path), "2026-10-26", "Tapo 2026-10-26 eventi (2)", "00:00 Movimento")
    folder = tmp_path / "root" / "Tapo 2026-10-26"
    assert isinstance(item, StoredItem) and item.name == "Tapo 2026-10-26 eventi (2).mp4" and item.size == 10
    assert (folder / item.name).read_bytes() == b"0123456789"
    assert (folder / "Tapo 2026-10-26 eventi (2).chapters.txt").read_text() == "00:00 Movimento"
    assert not list(folder.glob("*.part"))


def test_local_put_leaves_no_final_file_when_copy_fails(tmp_path, monkeypatch):
    import tapo_storage.local as local

    def boom(src, dst):
        Path(dst).write_bytes(b"half")
        raise OSError("disk full")

    monkeypatch.setattr(local.shutil, "copyfile", boom)
    storage = LocalStorage(tmp_path / "root", "Tapo")
    with pytest.raises(StorageError):
        storage.put(clip(tmp_path), "2026-10-26", "t", "")
    assert not list((tmp_path / "root").rglob("*.mp4")) and not list((tmp_path / "root").rglob("*.part"))


def test_local_days_and_list_day_newest_first(tmp_path):
    storage = LocalStorage(tmp_path / "root", "Tapo")
    for day in ("2026-10-24", "2026-10-26", "2026-10-25"):
        storage.put(clip(tmp_path), day, f"v {day}", "")
    (tmp_path / "root" / "other folder").mkdir()
    assert storage.days() == ["2026-10-26", "2026-10-25", "2026-10-24"]
    assert storage.days(limit=2) == ["2026-10-26", "2026-10-25"]
    assert [i.name for i in storage.list_day("2026-10-26")] == ["v 2026-10-26.mp4"]
    assert storage.list_day("2020-01-01") == []


def test_local_open_honours_range(tmp_path):
    storage = LocalStorage(tmp_path / "root", "Tapo")
    item = storage.put(clip(tmp_path), "2026-10-26", "v", "")
    chunks, total = storage.open("2026-10-26", item.name, start=2, end=5)
    assert b"".join(chunks) == b"2345" and total == 10
    chunks, total = storage.open("2026-10-26", item.name)
    assert b"".join(chunks) == b"0123456789"


def test_local_open_refuses_names_outside_the_folder(tmp_path):
    storage = LocalStorage(tmp_path / "root", "Tapo")
    storage.put(clip(tmp_path), "2026-10-26", "v", "")
    for bad in ("../x.mp4", "..", "a/b.mp4", "missing.mp4"):
        with pytest.raises(StorageError):
            storage.open("2026-10-26", bad)


def test_local_check_writes_and_removes_probe(tmp_path):
    storage = LocalStorage(tmp_path / "root", "Tapo")
    storage.check()
    assert list((tmp_path / "root").iterdir()) == []


def test_local_check_reports_unwritable_root(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(StorageError):
        LocalStorage(blocker / "root", "Tapo").check()


def test_make_storage_rejects_unknown_kind():
    with pytest.raises(StorageError):
        make_storage("floppy", {}, None)


def test_make_storage_builds_local(tmp_path):
    storage = make_storage("local", {"root": str(tmp_path), "prefix": "Cam"}, None)
    assert isinstance(storage, LocalStorage) and storage.kind == "local" and base.day_folder("Cam", "2026-01-02") == "Cam 2026-01-02"
