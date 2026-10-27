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
from typing import Callable, Optional

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
    on_token: Optional[Callable] = field(default=None, repr=False, compare=False)  # saves a renewed OneDrive token

    def signature(self):
        """Changes whenever the destination changes; reveals nothing about the secrets. A refresh token that Microsoft
        renews is still the same destination, so it is left out."""
        config = {k: v for k, v in self.config.items() if k != "refresh_token"}
        text = json.dumps([self.kind, config], sort_keys=True)
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
    if kind == "local":
        return Resolved("local", {}, "own")
    try:
        config = json.loads(vault.decrypt(row["config_enc"] or ""))
        if not isinstance(config, dict):
            raise ValueError("not an object")
    except (VaultError, ValueError, TypeError, AttributeError):
        return _fallback(default, f"Your saved {kind} settings cannot be read: the server default is used until you save them again.")
    if kind == "onedrive":
        if not config.get("client_id") and not settings.onedrive_enabled:
            return _fallback(default, "OneDrive is not set up on this server any more: the server default is used.")

        def save(token, config=config):  # Microsoft hands out a new refresh token now and then: keep it
            db.save_storage_config(user_id, "onedrive", vault.encrypt(json.dumps({**config, "refresh_token": token})))
        return Resolved(kind, config, "own", on_token=save)
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
    if resolved.source == "server" or resolved.kind == "local":   # the server's own disk: one folder per user and camera
        owner, camera = ctx.data_dir.parent.name, ctx.data_dir.name
        if resolved.kind == "local":
            config["root"] = str(Path(config.get("root") or settings.local_root) / owner / camera)
        else:
            config["folder"] = "/".join(part for part in (config.get("folder", ""), owner, camera) if part)
    elif resolved.kind == "webdav":  # a user-chosen address must not lead into the server's own network
        host_check = lambda host: checked_host(host, settings.allow_private_hosts, resolver)  # noqa: E731
    config["prefix"] = ctx.playlist_prefix
    if resolved.kind == "onedrive":
        # the person's own Microsoft app when they brought one, else the server's
        config.update(client_id=config.get("client_id") or settings.ms_client_id,
                      client_secret=config.get("client_secret") or settings.ms_client_secret,
                      tenant=config.get("tenant") or settings.ms_tenant, on_token=resolved.on_token)
        config.pop("account", None)
        return _onedrive(config, settings)
    return make_storage(resolved.kind, config, ctx, host_check)


def _onedrive(config, settings):
    """Built here, not by make_storage: the Microsoft addresses come from the server settings only."""
    from tapo_storage.onedrive import OneDriveStorage
    return OneDriveStorage(config["client_id"], config["client_secret"], config.get("refresh_token", ""), config["prefix"],
                           config.get("folder", ""), config["tenant"], config.get("on_token"),
                           graph_url=settings.ms_graph_url, login_url=settings.ms_login_url)
