"""Streams a recording to its owner (or an administrator), with HTTP Range support so the player can seek.

The route never takes a path: the day and the name must match a strict pattern AND appear in the destination's own
listing, and every request checks who owns the camera. YouTube recordings are not served from here at all.
"""
import logging
import re
from types import SimpleNamespace

from flask import Response, abort, request

from tapo_storage.base import DAY_RE, StorageError

from .cameras import camera_data_dir
from .sources import source_resolved
from .storage import build, resolve

log = logging.getLogger("tapo.web")
NAME_RE = re.compile(r"^[A-Za-z0-9 ._()-]+\.mp4$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
PREFIX = "Tapo"


def parse_range(header, size):
    """(start, end) inclusive for a single satisfiable range; ValueError for anything else."""
    match = RANGE_RE.match(header.strip())  # several ranges contain a comma and do not match
    if not match:
        raise ValueError("unsupported range")
    first, last = match.groups()
    if not first and not last:
        raise ValueError("empty range")
    if not first:  # the last N bytes
        count = int(last)
        if count == 0:
            raise ValueError("empty suffix")
        start, end = max(0, size - count), size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if last and int(last) < start:
            raise ValueError("backwards range")
    if start >= size:
        raise ValueError("beyond the end")
    return start, end


def register(app):
    state = app.extensions["tapo"]
    settings, db, vault = state["settings"], state["db"], state["vault"]

    @app.get("/media/<int:camera_id>/<day>/<name>")
    @app.login_required
    def media(camera_id, day, name):
        user = app.current_user()
        camera = db.camera_by_id(camera_id)
        if camera is None or (camera["user_id"] != user["id"] and user["role"] != "admin"):
            abort(404)
        if not DAY_RE.fullmatch(day) or not NAME_RE.fullmatch(name):
            abort(404)
        selector = request.args.get("s")
        if selector is None:
            resolved = resolve(settings, db, vault, camera["user_id"])
        else:  # a video of an earlier destination: the id is checked against the camera owner's own sources
            resolved = source_resolved(db, vault, camera["user_id"], int(selector)) if selector.isdigit() and len(selector) < 10 else None
        if resolved is None or resolved.kind == "youtube":
            abort(404)
        ctx = SimpleNamespace(data_dir=camera_data_dir(settings, camera), playlist_prefix=PREFIX)
        try:
            storage = build(resolved, settings, ctx, state["resolver"])
            item = next((i for i in storage.list_day(day) if i.name == name), None)
            if item is None:
                abort(404)
            size = item.size
            header = request.headers.get("Range")
            if header and size is not None:
                try:
                    start, end = parse_range(header, size)
                except ValueError:
                    return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
                status = 206
            else:
                start, end, status = 0, (size - 1 if size else None), 200
            chunks, total = storage.open(day, name, start, end)
        except StorageError as err:
            log.warning("camera %s: cannot serve %s: %s", camera_id, name, err)
            abort(404)
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"}
        if size is not None:
            length = (end - start + 1) if size else 0
            headers["Content-Length"] = str(length)
            if status == 206:
                headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        return Response(chunks, status=status, headers=headers, mimetype="video/mp4", direct_passthrough=True)
