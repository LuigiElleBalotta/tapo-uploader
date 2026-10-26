import json
import time as clock
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from tapo_web.dashboard import PlaylistCache, ago, camera_cards, list_playlists
from tests.conftest import Env


class Call:
    def __init__(self, owner, key, payload):
        self.owner, self.key, self.payload = owner, key, payload

    def execute(self):
        self.owner.calls.append(self.key)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeYoutube:
    """Just enough of the YouTube client: pages of playlists and the items of each playlist."""

    def __init__(self, playlists_pages, items=None, error=None):
        self.pages, self.items, self.error, self.calls = playlists_pages, items or {}, error, []

    def playlists(self):
        return self

    def playlistItems(self):
        return SimpleNamespace(list=self.list_items)

    def list(self, part=None, mine=None, maxResults=None, pageToken=None):
        if self.error:
            return Call(self, "playlists", self.error)
        return Call(self, "playlists", self.pages[0 if pageToken is None else int(pageToken)])

    def list_items(self, part=None, playlistId=None, maxResults=None):
        return Call(self, f"items:{playlistId}", {"items": self.items.get(playlistId, [])})


def playlist(pid, title):
    return {"id": pid, "snippet": {"title": title}}


def item(video_id, title, thumb=None, published="2026-10-26T20:00:00Z"):
    thumbs = {"medium": {"url": thumb or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"}}
    return {"snippet": {"title": title, "publishedAt": published, "thumbnails": thumbs,
                        "resourceId": {"videoId": video_id}}}


# ----- listing -----

def test_list_playlists_filters_by_prefix_and_sorts():
    yt = FakeYoutube([
        {"items": [playlist("A", "Tapo 2026-10-25"), playlist("H", "Holiday"), playlist("N", "Tapo notes")],
         "nextPageToken": "1"},
        {"items": [playlist("B", "Tapo 2026-10-26")]},
    ], {"A": [item("v1", "old")], "B": [item("v2", "new")]})
    result = list_playlists(yt, "Tapo")
    assert [p["title"] for p in result] == ["Tapo 2026-10-26", "Tapo 2026-10-25"]
    assert result[0]["videos"][0]["title"] == "new"


def test_urls_point_to_youtube():
    yt = FakeYoutube([{"items": [playlist("PL1", "Tapo 2026-10-26")]}], {"PL1": [item("abc123", "clip")]})
    result = list_playlists(yt, "Tapo")[0]
    assert result["url"] == "https://www.youtube.com/playlist?list=PL1"
    assert result["videos"][0]["url"] == "https://www.youtube.com/watch?v=abc123"


def test_limit_and_foreign_thumbnails():
    yt = FakeYoutube([{"items": [playlist(str(i), f"Tapo 2026-10-{10 + i}") for i in range(5)]}],
                     {"4": [item("v", "clip", thumb="https://evil.example.com/x.jpg")]})
    result = list_playlists(yt, "Tapo", limit=2)
    assert [p["title"] for p in result] == ["Tapo 2026-10-14", "Tapo 2026-10-13"]
    assert result[0]["videos"][0]["thumbnail"] is None  # only YouTube's own image host is allowed


def test_playlist_cache_avoids_repeat_calls():
    now = [1000.0]
    cache = PlaylistCache(ttl=300, clock=lambda: now[0])
    loads = []
    loader = lambda: loads.append(1) or ["data"]
    assert cache.get(("u", 1), loader) == ["data"] and cache.get(("u", 1), loader) == ["data"]
    assert len(loads) == 1
    now[0] += 301
    cache.get(("u", 1), loader)
    assert len(loads) == 2
    cache.get(("u", 2), loader)  # another key is another entry
    assert len(loads) == 3


def test_ago():
    assert ago(None, 1000) == "never"
    assert ago(990, 1000) == "just now"
    assert ago(1000 - 5 * 60, 1000) == "5 min ago"
    assert ago(1000 - 3 * 3600, 1000) == "3 h ago"
    assert ago(1000 - 2 * 86400, 1000) == "2 days ago"


# ----- the page -----

def token_json(token):
    return json.dumps({"token": token, "refresh_token": "r", "client_id": "c", "client_secret": "s",
                       "token_uri": "https://oauth2.googleapis.com/token",
                       "scopes": ["https://www.googleapis.com/auth/youtube"], "expiry": "2099-01-01T00:00:00Z"})


def make(tmp_path):
    env = Env(tmp_path, max_cameras_per_user=2)
    env.alice = env.add_local_user("alice", "correct horse")
    env.bob = env.add_local_user("bob", "another pass")
    return env


def link(env, user_id, token):
    env.db.save_youtube_link(user_id, env.vault.encrypt(token_json(token)), f"{token} channel", "shared")


def camera(env, user_id, name):
    return env.db.add_camera(user_id, name, "cam.example.com", 38554, 38020, "stream1",
                             env.vault.encrypt("u"), env.vault.encrypt("p"))


def page(env, username, password):
    client = env.app.test_client()
    env.login(username, password, client=client)
    return client.get("/")


def test_dashboard_shows_only_own_cameras_and_playlists(tmp_path):
    env = make(tmp_path)
    camera(env, env.alice, "Alice camera")
    camera(env, env.bob, "Bob camera")
    link(env, env.alice, "token-alice")
    link(env, env.bob, "token-bob")
    fakes = {"token-alice": FakeYoutube([{"items": [playlist("PA", "Tapo 2026-10-26")]}], {"PA": [item("va", "Alice clip")]}),
             "token-bob": FakeYoutube([{"items": [playlist("PB", "Tapo 2026-10-25")]}], {"PB": [item("vb", "Bob clip")]})}
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: fakes[credentials.token]
    text = page(env, "bob", "another pass").get_data(as_text=True)
    assert "Bob camera" in text and "Bob clip" in text and "Tapo 2026-10-25" in text
    assert "Alice camera" not in text and "Alice clip" not in text and "Tapo 2026-10-26" not in text


def test_dashboard_without_youtube_link_shows_connect_button(tmp_path):
    env = make(tmp_path)
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert "Connect YouTube" in text and "/youtube" in text


def test_dashboard_offers_add_camera_only_under_the_limit(tmp_path):
    env = make(tmp_path)
    assert "/cameras/new" in page(env, "alice", "correct horse").get_data(as_text=True)
    camera(env, env.alice, "one")
    camera(env, env.alice, "two")
    assert "/cameras/new" not in page(env, "alice", "correct horse").get_data(as_text=True)


def test_youtube_error_shows_message_not_traceback(tmp_path):
    env = make(tmp_path)
    link(env, env.alice, "token-alice")
    error = HttpError(SimpleNamespace(status=500, reason="boom"), b"internal details 10.1.2.3")
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: FakeYoutube([], error=error)
    response = page(env, "alice", "correct horse")
    text = response.get_data(as_text=True)
    assert response.status_code == 200 and "YouTube" in text
    assert "Traceback" not in text and "10.1.2.3" not in text


def test_revoked_link_asks_to_reconnect(tmp_path, monkeypatch):
    env = make(tmp_path)
    expired = token_json("old").replace("2099-01-01T00:00:00Z", "2000-01-01T00:00:00Z")
    env.db.save_youtube_link(env.alice, env.vault.encrypt(expired), "chan", "shared")

    def refuse(credentials):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr("tapo_web.youtube_link._refresh", refuse)
    response = page(env, "alice", "correct horse")
    assert response.status_code == 200 and "Connect YouTube again" in response.get_data(as_text=True)


def test_camera_cards_show_status(tmp_path):
    env = make(tmp_path)
    cid = camera(env, env.alice, "Garden")
    env.db.set_camera_status(cid, enabled=1, status="online", last_motion=clock.time() - 120, last_error="old problem")
    supervisor = SimpleNamespace(status=lambda camera_id: {"running": True, "pending_clips": 3})
    card = camera_cards(env.db, supervisor, env.alice)[0]
    assert card["name"] == "Garden" and card["online"] is True and card["pending_clips"] == 3
    assert card["last_motion"] == "2 min ago" and card["last_upload"] == "never" and card["last_error"] == "old problem"
    env.app.extensions["tapo"]["supervisor"] = supervisor
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert "Garden" in text and "online" in text and "2 min ago" in text and "old problem" in text


def test_titles_are_escaped(tmp_path):
    env = make(tmp_path)
    camera(env, env.alice, "<script>alert(1)</script>")
    link(env, env.alice, "token-alice")
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: FakeYoutube(
        [{"items": [playlist("P", "Tapo 2026-10-26")]}], {"P": [item("v", "<img src=x onerror=alert(1)>")]})
    text = page(env, "alice", "correct horse").get_data(as_text=True)
    assert "<script>alert(1)" not in text and "<img src=x" not in text


def test_dashboard_has_no_edit_or_delete_video_controls(tmp_path):
    env = make(tmp_path)
    link(env, env.alice, "token-alice")
    env.app.extensions["tapo"]["youtube_builder"] = lambda credentials: FakeYoutube(
        [{"items": [playlist("P", "Tapo 2026-10-26")]}], {"P": [item("v", "clip")]})
    text = page(env, "alice", "correct horse").get_data(as_text=True).lower()
    assert "delete video" not in text and "/videos" not in text and "remove from playlist" not in text
