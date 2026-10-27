"""OneDrive backend (Microsoft Graph, app folder) against an in-process fake."""
import pytest

from tapo_storage.base import StorageError, make_storage
from tapo_storage.onedrive import OneDriveStorage
from tests.fakes_graph import FakeGraph

CHUNK = 320 * 1024


@pytest.fixture
def graph():
    server = FakeGraph()
    yield server
    server.stop()


def storage(graph, **kw):
    return OneDriveStorage(kw.pop("client_id", "cid"), kw.pop("client_secret", "csecret"), kw.pop("refresh_token", "refresh-1"),
                           "Tapo", graph_url=graph.graph_url, login_url=graph.base, **kw)


def source(tmp_path, data=b"0123456789"):
    path = tmp_path / "day.mp4"
    path.write_bytes(data)
    return path


def test_check_writes_and_removes_a_probe_in_the_app_folder(graph):
    storage(graph).check()
    assert graph.files == {} and [m for m, p, _ in graph.requests if m in ("PUT", "DELETE")] == ["PUT", "DELETE"]
    form = graph.token_calls[0]
    assert form["grant_type"] == "refresh_token" and form["scope"] == "Files.ReadWrite.AppFolder offline_access"


def test_wrong_secret_or_revoked_access_says_to_connect_again_without_leaking_microsoft_text(graph):
    for bad in (dict(client_secret="wrong"), dict(refresh_token="stolen")):
        with pytest.raises(StorageError, match="connect OneDrive again") as err:
            storage(graph, **bad).check()
        assert "AADSTS" not in str(err.value) and "secret" not in str(err.value).lower().replace("client secret", "")
    graph.mode = "revoked"
    with pytest.raises(StorageError, match="connect OneDrive again"):
        storage(graph).check()


def test_unreachable_microsoft_is_a_clean_error(graph):
    graph.stop()
    with pytest.raises(StorageError, match="cannot reach"):
        storage(graph).check()


def test_put_uploads_in_a_session_and_adds_the_chapters(graph, tmp_path):
    item = storage(graph).put(source(tmp_path), "2026-10-26", "Tapo 2026-10-26 eventi (2)", "00:00 Movimento")
    assert graph.files["Tapo 2026-10-26/Tapo 2026-10-26 eventi (2).mp4"] == b"0123456789"
    assert graph.files["Tapo 2026-10-26/Tapo 2026-10-26 eventi (2).chapters.txt"] == b"00:00 Movimento"
    assert "Tapo 2026-10-26" in graph.dirs and graph.sessions == {}
    assert item.name == "Tapo 2026-10-26 eventi (2).mp4" and item.size == 10 and item.url.startswith("https://onedrive.example/")


def test_a_big_file_goes_up_in_chunks_that_are_multiples_of_320_kib(graph, tmp_path):
    data = bytes(range(256)) * 3000                      # 768,000 bytes
    storage(graph, chunk=CHUNK).put(source(tmp_path, data), "2026-10-26", "big", "")
    assert graph.files["Tapo 2026-10-26/big.mp4"] == data
    assert graph.session_chunks == [(0, CHUNK - 1, len(data)), (CHUNK, 2 * CHUNK - 1, len(data)),
                                    (2 * CHUNK, len(data) - 1, len(data))]


def test_an_empty_file_is_uploaded_without_a_session(graph, tmp_path):
    storage(graph).put(source(tmp_path, b""), "2026-10-26", "empty", "")
    assert graph.files["Tapo 2026-10-26/empty.mp4"] == b"" and graph.session_chunks == []


def test_a_broken_upload_leaves_no_file_and_cancels_the_session(graph, tmp_path):
    graph.mode = "break_upload"
    src = source(tmp_path, b"x" * 1000)
    with pytest.raises(StorageError):
        storage(graph).put(src, "2026-10-26", "v", "")
    assert not [p for p in graph.files if p.endswith(".mp4")] and graph.sessions == {} and src.exists()


def test_the_login_header_goes_to_graph_only_never_to_upload_or_download_urls(graph, tmp_path):
    s = storage(graph)
    item = s.put(source(tmp_path), "2026-10-26", "v", "")
    b"".join(s.open("2026-10-26", item.name)[0])
    direct = [auth for m, p, auth in graph.requests if p.startswith(("/upload/", "/dl/"))]
    assert direct and not any(direct)


def test_one_token_serves_many_calls_and_a_rotated_refresh_token_is_handed_back(graph, tmp_path):
    graph.rotate = True
    saved = []
    s = storage(graph, on_token=saved.append)
    s.put(source(tmp_path), "2026-10-26", "a", "")
    s.put(source(tmp_path), "2026-10-26", "b", "")
    assert len(graph.token_calls) == 1 and saved == ["refresh-2"]
    assert s.refresh_token == "refresh-2"


def test_an_access_token_refused_once_is_renewed_and_the_call_retried(graph, tmp_path):
    graph.mode = "expire"
    storage(graph).put(source(tmp_path), "2026-10-26", "v", "")
    assert graph.files["Tapo 2026-10-26/v.mp4"] == b"0123456789" and len(graph.token_calls) == 2


def test_hostile_characters_in_the_title_cannot_escape_the_folder(graph, tmp_path):
    item = storage(graph).put(source(tmp_path), "2026-10-26", "Cámara #1 ? 12:30/..\\x", "")
    assert "/" not in item.name and "\\" not in item.name and ":" not in item.name
    assert [i.name for i in storage(graph).list_day("2026-10-26")] == [item.name]


def test_folder_option_nests_everything_and_rejects_dots(graph, tmp_path):
    storage(graph, folder="cams/1").put(source(tmp_path), "2026-10-26", "v", "")
    assert "cams/1/Tapo 2026-10-26/v.mp4" in graph.files
    with pytest.raises(StorageError):
        storage(graph, folder="a/../b")


def test_days_and_list_day_follow_the_pages(graph, tmp_path):
    s = storage(graph)
    for day in ("2026-10-24", "2026-10-26", "2026-10-25"):
        s.put(source(tmp_path), day, f"v {day}", "d")
    graph.dirs.add("not a day")
    assert s.days() == ["2026-10-26", "2026-10-25", "2026-10-24"] and s.days(limit=1) == ["2026-10-26"]
    items = s.list_day("2026-10-26")
    assert [(i.name, i.size) for i in items] == [("v 2026-10-26.mp4", 10)] and items[0].modified.startswith("2027-01-15")
    assert s.list_day("2020-01-01") == []


def test_open_returns_the_requested_range_and_the_total_size(graph, tmp_path):
    s = storage(graph)
    item = s.put(source(tmp_path), "2026-10-26", "v", "")
    chunks, total = s.open("2026-10-26", item.name, start=2, end=5)
    assert b"".join(chunks) == b"2345" and total == 10
    chunks, total = s.open("2026-10-26", item.name)
    assert b"".join(chunks) == b"0123456789" and total == 10


def test_open_refuses_names_with_separators_and_missing_files(graph):
    for bad in ("../x.mp4", "a/b.mp4", "..", ""):
        with pytest.raises(StorageError):
            storage(graph).open("2026-10-26", bad)
    with pytest.raises(StorageError):
        storage(graph).open("2026-10-26", "missing.mp4")


def test_make_storage_builds_onedrive_from_the_config():
    s = make_storage("onedrive", {"client_id": "c", "client_secret": "s", "refresh_token": "r", "folder": "f"}, None)
    assert isinstance(s, OneDriveStorage) and s.kind == "onedrive"
    with pytest.raises(StorageError):
        make_storage("onedrive", {"client_id": "c", "client_secret": "s"}, None)       # no refresh token: not connected
