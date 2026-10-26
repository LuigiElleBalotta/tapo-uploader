"""WebDAV backend against an in-process fake server."""
import pytest

from tapo_storage import webdav
from tapo_storage.base import StorageError, make_storage
from tapo_storage.webdav import WebDAVStorage
from tests.fakes import FakeDAV


@pytest.fixture
def dav():
    server = FakeDAV()
    yield server
    server.stop()


def storage(dav, **kw):
    return WebDAVStorage(dav.url, kw.pop("user", "u"), kw.pop("password", "p"), "Tapo", **kw)


def source(tmp_path, data=b"0123456789"):
    path = tmp_path / "day.mp4"
    path.write_bytes(data)
    return path


def test_check_creates_and_removes_probe(dav):
    storage(dav, folder="cams").check()
    assert dav.files == {} and "/dav/cams" in dav.dirs
    assert [m for m, _, _ in dav.requests if m in ("PUT", "DELETE")] == ["PUT", "DELETE"]


def test_check_reports_wrong_password(dav):
    with pytest.raises(StorageError, match="login"):
        storage(dav, password="nope").check()


def test_check_reports_unreachable_server(dav):
    dav.stop()
    with pytest.raises(StorageError, match="cannot reach"):
        storage(dav).check()


def test_put_makes_day_folder_uploads_part_then_moves(dav, tmp_path):
    item = storage(dav).put(source(tmp_path), "2026-10-26", "Tapo 2026-10-26 eventi (2)", "00:00 Movimento")
    folder = "/dav/Tapo 2026-10-26"
    assert dav.files[f"{folder}/Tapo 2026-10-26 eventi (2).mp4"] == b"0123456789"
    assert dav.files[f"{folder}/Tapo 2026-10-26 eventi (2).chapters.txt"] == b"00:00 Movimento"
    assert not [p for p in dav.files if p.endswith(".part")]
    puts = [path for m, path, _ in dav.requests if m == "PUT"]
    assert puts[0].endswith(".mp4.part") and any(m == "MOVE" for m, _, _ in dav.requests)
    assert item.name == "Tapo 2026-10-26 eventi (2).mp4" and item.size == 10


def test_put_with_spaces_unicode_and_hostile_characters_in_title(dav, tmp_path):
    s = storage(dav)
    item = s.put(source(tmp_path), "2026-10-26", "Cámara #1 ? 12:30/..\\x", "")
    raw = [path for m, path, _ in dav.requests if m == "PUT"][0]
    assert "%20" in raw and "#" not in raw and "?" not in raw and "%C3%A1" in raw
    assert [i.name for i in s.list_day("2026-10-26")] == [item.name]


def test_put_does_not_follow_redirect(dav, tmp_path):
    dav.mode = "redirect"
    with pytest.raises(StorageError, match="redirect"):
        storage(dav).put(source(tmp_path), "2026-10-26", "v", "")
    assert dav.files == {}


def test_interrupted_put_leaves_no_final_file(dav, tmp_path):
    dav.mode = "drop_on_put"
    src = source(tmp_path, b"x" * 200_000)
    with pytest.raises(StorageError):
        storage(dav).put(src, "2026-10-26", "v", "")
    assert not [p for p in dav.files if p.endswith(".mp4")] and src.exists()


def test_put_streams_without_loading_file_in_memory(dav, tmp_path, monkeypatch):
    sizes = []

    class Spy:
        def __init__(self, handle):
            self.handle = handle

        def read(self, size=-1):
            sizes.append(size)
            return self.handle.read(size)

        def close(self):
            self.handle.close()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    monkeypatch.setattr(webdav, "_open_source", lambda path: Spy(open(path, "rb")))
    storage(dav).put(source(tmp_path, b"y" * 3_000_000), "2026-10-26", "big", "")
    assert sizes and all(0 < size <= 1024 * 1024 for size in sizes)
    assert len(dav.files["/dav/Tapo 2026-10-26/big.mp4"]) == 3_000_000


def test_days_and_list_day_via_propfind(dav, tmp_path):
    s = storage(dav)
    for day in ("2026-10-24", "2026-10-26", "2026-10-25"):
        s.put(source(tmp_path), day, f"v {day}", "d")
    dav.dirs.add("/dav/not a day")
    assert s.days() == ["2026-10-26", "2026-10-25", "2026-10-24"] and s.days(limit=1) == ["2026-10-26"]
    items = s.list_day("2026-10-26")
    assert [(i.name, i.size) for i in items] == [("v 2026-10-26.mp4", 10)]
    assert items[0].url == f"{dav.url}/Tapo%202026-10-26/v%202026-10-26.mp4" and items[0].modified.startswith("2027-")
    assert s.list_day("2020-01-01") == []


def test_open_forwards_range_and_returns_total(dav, tmp_path):
    s = storage(dav)
    item = s.put(source(tmp_path), "2026-10-26", "v", "")
    chunks, total = s.open("2026-10-26", item.name, start=2, end=5)
    assert b"".join(chunks) == b"2345" and total == 10
    assert [h["Range"] for m, _, h in dav.requests if m == "GET"] == ["bytes=2-5"]
    chunks, total = s.open("2026-10-26", item.name)
    assert b"".join(chunks) == b"0123456789"


def test_open_refuses_names_with_separators(dav):
    for bad in ("../x.mp4", "a/b.mp4", ".."):
        with pytest.raises(StorageError):
            storage(dav).open("2026-10-26", bad)


def test_host_check_result_is_the_address_connected_to(dav):
    url = f"http://dav.example.test:{dav.port}/dav"
    s = WebDAVStorage(url, "u", "p", "Tapo", host_check=lambda host: "127.0.0.1")
    s.check()
    assert dav.requests[0][2]["Host"] == f"dav.example.test:{dav.port}"


def test_private_host_check_failure_becomes_storage_error(dav):
    def refuse(host):
        raise ValueError("that address is private")

    with pytest.raises(StorageError, match="private"):
        WebDAVStorage(dav.url, "u", "p", "Tapo", host_check=refuse).check()
    assert dav.requests == []


def test_bad_url_and_folder_are_rejected():
    for url in ("ftp://x/y", "not a url", ""):
        with pytest.raises(StorageError):
            WebDAVStorage(url, "u", "p", "Tapo")
    with pytest.raises(StorageError):
        WebDAVStorage("https://x/dav", "u", "p", "Tapo", folder="a/../b")


def test_make_storage_builds_webdav(dav):
    s = make_storage("webdav", {"url": dav.url, "user": "u", "password": "p", "folder": "f"}, None)
    assert isinstance(s, WebDAVStorage) and s.kind == "webdav"
