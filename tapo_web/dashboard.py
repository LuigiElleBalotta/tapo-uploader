"""The personal page: the user's camera status and the videos on their own YouTube channel (read only)."""
import logging
import re
import threading
import time
from urllib.parse import quote

from flask import render_template

from .youtube_link import LinkRevoked, youtube_for_user

log = logging.getLogger("tapo.web")
PLAYLIST_PREFIX = "Tapo"
MAX_PLAYLIST_PAGES = 4
THUMBNAIL_HOST = "https://i.ytimg.com/"


def ago(timestamp, now=None):
    """'3 min ago' for an epoch time, 'never' for None."""
    if timestamp is None:
        return "never"
    seconds = (now if now is not None else time.time()) - timestamp
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


class PlaylistCache:
    """Remember what YouTube answered for a few minutes: opening the page must not spend quota every time."""

    def __init__(self, ttl=300, clock=time.time):
        self.ttl, self.clock, self._data, self._lock = ttl, clock, {}, threading.Lock()

    def get(self, key, loader):
        with self._lock:
            hit = self._data.get(key)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
        value = loader()
        with self._lock:
            self._data[key] = (self.clock(), value)
        return value


def _thumbnail(snippet):
    thumbs = snippet.get("thumbnails") or {}
    for size in ("medium", "default", "high"):
        url = (thumbs.get(size) or {}).get("url")
        if url:
            return url if url.startswith(THUMBNAIL_HOST) else None  # only YouTube's own image host
    return None


def list_playlists(yt, prefix=PLAYLIST_PREFIX, limit=10):
    """The user's daily playlists ('Tapo YYYY-MM-DD'), newest first, each with its videos."""
    pattern = re.compile(rf"^{re.escape(prefix)} \d{{4}}-\d{{2}}-\d{{2}}$")
    found, token = [], None
    for _ in range(MAX_PLAYLIST_PAGES):
        reply = yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=token).execute()
        found += [p for p in reply.get("items", []) if pattern.match(p["snippet"]["title"])]
        token = reply.get("nextPageToken")
        if not token:
            break
    found.sort(key=lambda p: p["snippet"]["title"], reverse=True)
    result = []
    for entry in found[:limit]:
        items = yt.playlistItems().list(part="snippet", playlistId=entry["id"], maxResults=25).execute().get("items", [])
        videos = []
        for video in items:
            snippet = video["snippet"]
            video_id = snippet["resourceId"]["videoId"]
            videos.append({"id": video_id, "title": snippet.get("title", ""), "thumbnail": _thumbnail(snippet),
                           "published": (snippet.get("publishedAt") or "")[:10],
                           "url": f"https://www.youtube.com/watch?v={quote(video_id, safe='')}"})
        result.append({"id": entry["id"], "title": entry["snippet"]["title"],
                       "url": f"https://www.youtube.com/playlist?list={quote(entry['id'], safe='')}",
                       "videos": videos})
    return result


def camera_cards(db, supervisor, user_id, now=None):
    cards = []
    for camera in db.cameras_for_user(user_id):
        live = supervisor.status(camera["id"]) if supervisor else {"running": False, "pending_clips": 0}
        cards.append({
            "id": camera["id"], "name": camera["name"], "enabled": bool(camera["enabled"]),
            "status": camera["status"], "running": live["running"],
            "online": camera["status"] == "online" and live["running"],
            "pending_clips": live["pending_clips"],
            "last_motion": ago(camera["last_motion"], now), "last_upload": ago(camera["last_upload"], now),
            "last_error": camera["last_error"],
        })
    return cards


def register(app):
    state = app.extensions["tapo"]
    settings, db, vault = state["settings"], state["db"], state["vault"]
    state.setdefault("supervisor", None)
    cache = state.setdefault("playlist_cache", PlaylistCache())

    def youtube_section(user_id):
        link = db.youtube_link(user_id)
        if not link:
            return {"linked": False}
        try:
            def load():
                yt = youtube_for_user(db, vault, settings, user_id, builder=state["youtube_builder"])
                return list_playlists(yt)
            return {"linked": True, "channel": link["channel_title"],
                    "playlists": cache.get((user_id, link["linked_at"]), load)}
        except LinkRevoked:
            return {"linked": True, "channel": link["channel_title"], "problem": "revoked"}
        except Exception as err:  # YouTube down, quota, network: the page must still open
            log.warning("could not list playlists: %s", type(err).__name__)
            return {"linked": True, "channel": link["channel_title"], "problem": "error"}

    @app.get("/")
    @app.login_required
    def home():
        user = app.current_user()
        cameras = camera_cards(db, state["supervisor"], user["id"])
        return render_template("dashboard.html", cameras=cameras, youtube=youtube_section(user["id"]),
                               can_add_camera=len(cameras) < settings.max_cameras_per_user)
