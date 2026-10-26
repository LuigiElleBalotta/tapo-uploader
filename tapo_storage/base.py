"""The storage interface every destination implements, and the naming rules they share."""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CHUNK = 1024 * 1024
_MAX_NAME = 150


class StorageError(Exception):
    """A destination failed in a way worth showing to the user as is."""


@dataclass(frozen=True)
class StoredItem:
    name: str
    day: str
    size: Optional[int]
    modified: str
    url: Optional[str]


def safe_filename(title):
    """A remote file name that is safe on every destination: no separators, colons or control characters."""
    stem = title[:-4] if title.lower().endswith(".mp4") else title
    stem = stem.replace(":", "-")
    stem = "".join(c if (c.isalnum() or c in " ._-()") else "_" for c in stem)
    stem = stem.lstrip(". ")[:_MAX_NAME].rstrip(" ")
    return f"{stem or 'video'}.mp4"


def day_folder(prefix, day):
    return f"{prefix} {day}"


DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def sidecar_name(name):
    """The chapters file next to a video: 'x.mp4' -> 'x.chapters.txt'."""
    return f"{name[:-4] if name.endswith('.mp4') else name}.chapters.txt"


class Storage:
    kind = ""
    uses_quota = False          # only YouTube spends the shared API quota
    browsable = True            # days()/list_day()/open() work

    def connect(self) -> None:
        """Get ready to upload; raises what the destination raises (a revoked token, ...). Called before the
        day's clips are joined, so a destination that is not reachable costs nothing."""

    def check(self) -> None:
        raise NotImplementedError

    def put(self, path: Path, day: str, title: str, description: str = "") -> StoredItem:
        raise NotImplementedError

    def days(self, limit: int = 10) -> list:
        raise StorageError(f"{self.kind} cannot list recordings")

    def list_day(self, day: str) -> list:
        raise StorageError(f"{self.kind} cannot list recordings")

    def open(self, day: str, name: str, start: int = 0, end: Optional[int] = None) -> tuple:
        """(chunks, total size); `end` is inclusive, None means the end of the file."""
        raise StorageError(f"{self.kind} cannot play recordings")


def make_storage(kind, config, ctx, host_check=None):
    prefix = config.get("prefix") or (ctx.playlist_prefix if ctx is not None else "Tapo")
    if kind == "local":
        from .local import LocalStorage
        return LocalStorage(Path(config["root"]), prefix)
    if kind == "rclone":
        from .rclone import RcloneStorage
        return RcloneStorage(config.get("remote", ""), config.get("config") or None, prefix, config.get("folder", ""))
    if kind == "webdav":
        from .webdav import WebDAVStorage
        return WebDAVStorage(config.get("url", ""), config.get("user", ""), config.get("password", ""), prefix,
                             config.get("folder", ""), host_check)
    if kind == "youtube":
        from .youtube import YouTubeStorage
        return YouTubeStorage(ctx, config.get("state"))
    raise StorageError(f"unknown storage kind '{kind}'")
