"""Clips that wait for the daily upload: listed on the personal page and streamed to their owner.

They sit on the server as `events/YYYYMMDD/HHMMSS.mp4`. The route never takes a path: the day and the name must
match a strict pattern, a link is never followed, and every request checks who owns the camera.
"""
import re
import time
from pathlib import Path

from flask import Response, abort, request

from .cameras import camera_data_dir
from .media import parse_range

DAY_DIR_RE = re.compile(r"^\d{8}$")
CLIP_RE = re.compile(r"^\d{6}\.mp4$")
STILL_WRITING_SECONDS = 120  # the recorder is still adding to a file touched more recently than this
CHUNK = 256 * 1024
GROUP_TITLE = "Waiting for upload"


def list_pending(events_dir, camera_id, label="", now=None):
    """One group with the finished, not yet uploaded clips (newest first), or [] when none waits."""
    if not events_dir.is_dir():
        return []
    now = now if now is not None else time.time()
    videos = []
    for day in events_dir.iterdir():
        if not DAY_DIR_RE.fullmatch(day.name) or day.is_symlink() or not day.is_dir():
            continue
        for path in day.iterdir():
            if not CLIP_RE.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            if now - info.st_mtime <= STILL_WRITING_SECONDS:
                continue
            stamp = f"{day.name[:4]}-{day.name[4:6]}-{day.name[6:]} {path.name[:2]}:{path.name[2:4]}"
            videos.append((day.name + path.name, {
                "kind": "file", "ref": f"/pending/{camera_id}/{day.name}/{path.name}", "title": f"Motion at {path.name[:2]}:{path.name[2:4]}",
                "thumbnail": None, "watchable": True, "pending": True, "published": stamp, "url": None,
                "size": size_label(info.st_size)}))
    if not videos:
        return []
    videos.sort(key=lambda entry: entry[0], reverse=True)
    return [{"id": f"pending-{camera_id}", "title": f"{label}{GROUP_TITLE}", "url": None, "videos": [v for _, v in videos]}]


def size_label(size):
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _chunks(path, start, end):
    with open(path, "rb") as handle:
        handle.seek(start)
        left = end - start + 1
        while left > 0:
            data = handle.read(min(CHUNK, left))
            if not data:
                return
            left -= len(data)
            yield data


def register(app):
    state = app.extensions["tapo"]
    settings, db = state["settings"], state["db"]

    @app.get("/pending/<int:camera_id>/<day>/<name>")
    @app.login_required
    def pending_clip(camera_id, day, name):
        user = app.current_user()
        camera = db.camera_by_id(camera_id)
        if camera is None or (camera["user_id"] != user["id"] and user["role"] != "admin"):
            abort(404)
        if not DAY_DIR_RE.fullmatch(day) or not CLIP_RE.fullmatch(name):
            abort(404)
        folder = camera_data_dir(settings, camera) / "events" / day
        path = folder / name
        if folder.is_symlink() or path.is_symlink() or not path.is_file():
            abort(404)
        size = path.stat().st_size
        header = request.headers.get("Range")
        if header:
            try:
                start, end = parse_range(header, size)
            except ValueError:
                return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            status = 206
        else:
            start, end, status = 0, size - 1, 200
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store",
                   "Content-Length": str(end - start + 1 if size else 0)}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(_chunks(path, start, end) if size else (), status=status, headers=headers, mimetype="video/mp4",
                        direct_passthrough=True)
