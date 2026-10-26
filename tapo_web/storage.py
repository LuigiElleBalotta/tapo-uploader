"""Which destination a user's recordings go to, and how it is built.

A user's own choice (YouTube or their own WebDAV) wins only when the server allows it; otherwise the server
default applies, and the user is told why. Destinations the server hosts for everyone (its WebDAV, local folder,
rclone remote) get one sub-folder per camera, so nobody can list another person's footage.
"""
import hashlib
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tapo_storage.base import make_storage

from .config import USER_STORAGE_KINDS
from .crypto import VaultError
from .security import checked_host


@dataclass(frozen=True)
class Resolved:
    kind: str
    config: dict = field(default_factory=dict, repr=False)  # holds secrets: never log it
    source: str = "server"                                  # "own" | "server"
    notice: Optional[str] = None

    def signature(self):
        """Changes whenever the destination changes; reveals nothing about the secrets."""
        text = json.dumps([self.kind, self.config], sort_keys=True)
        return hashlib.sha256(text.encode()).hexdigest()[:16]


def server_default(settings):
    kind = settings.storage
    if kind == "webdav":
        return Resolved(kind, {"url": settings.storage_webdav_url, "user": settings.storage_webdav_user,
                               "password": settings.storage_webdav_password, "folder": settings.storage_webdav_folder})
    if kind == "local":
        return Resolved(kind, {"root": settings.storage_local_root})
    if kind == "rclone":
        return Resolved(kind, {"remote": settings.rclone_remote, "config": settings.rclone_config,
                               "folder": settings.rclone_folder})
    return Resolved("youtube")


def resolve(settings, db, vault, user_id):
    row = db.storage_config(user_id)
    default = server_default(settings)
    if row is None:
        return default
    kind = row["kind"]
    if not settings.allow_own_storage:
        return _fallback(default, "Your own destination setting is not allowed on this server, so the server default is used.")
    if kind not in USER_STORAGE_KINDS or kind not in settings.storage_allowed:
        return _fallback(default, f"Your {kind} setting is no longer allowed: the server default is used.")
    if kind == "youtube":
        return Resolved("youtube", {}, "own")
    try:
        config = json.loads(vault.decrypt(row["config_enc"] or ""))
        if not isinstance(config, dict):
            raise ValueError("not an object")
    except (VaultError, ValueError, TypeError, AttributeError):
        return _fallback(default, f"Your saved {kind} settings cannot be read: the server default is used until you save them again.")
    return Resolved(kind, config, "own")


def _fallback(default, notice):
    return Resolved(default.kind, default.config, default.source, notice)


def build(resolved, settings, ctx, resolver=socket.getaddrinfo):
    """The Storage object for a camera, or None for YouTube (the context then uses the user's YouTube link).
    `ctx` needs `data_dir` (users/<user id>/camera-<id>) and `playlist_prefix`."""
    if resolved.kind == "youtube":
        return None
    config = dict(resolved.config)
    host_check = None
    if resolved.source == "server":
        owner, camera = ctx.data_dir.parent.name, ctx.data_dir.name
        if resolved.kind == "local":
            config["root"] = str(Path(config["root"]) / owner / camera)
        else:
            config["folder"] = "/".join(part for part in (config.get("folder", ""), owner, camera) if part)
    elif resolved.kind == "webdav":  # a user-chosen address must not lead into the server's own network
        host_check = lambda host: checked_host(host, settings.allow_private_hosts, resolver)  # noqa: E731
    config["prefix"] = ctx.playlist_prefix
    return make_storage(resolved.kind, config, ctx, host_check)
