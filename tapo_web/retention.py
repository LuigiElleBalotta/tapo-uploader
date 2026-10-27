"""Rotation of the videos kept on the server.

Only videos held by a *local* destination are ever deleted here. Videos in YouTube, OneDrive, WebDAV or any other
cloud are never touched, and neither are the clips still waiting for their upload (they are retried, and meanwhile
they use space on this server). Three rules, applied in this order:

1. age: videos older than the person's "keep days" (their own value, else the server's; 0 = forever);
2. the person's space limit (their own value from an administrator, else the server's; 0 = unlimited), deleting from
   the oldest video to the newest until it fits;
3. the disk: when the free space falls below DISK_MIN_FREE_GB, the oldest local videos of anyone go until there is room.
"""
import logging
import shutil
from datetime import timedelta
from types import SimpleNamespace

from tapo_storage.base import StorageError
from tapo_storage.local import LocalStorage

from .cameras import camera_data_dir
from .sources import remembered
from .storage import build, resolve

log = logging.getLogger("tapo.web")
GB = 1024 ** 3
PREFIX = "Tapo"
NEARLY_FULL = 0.95


def effective_limits(settings, row):
    """(keep days, space limit in GB) for a person: their own values, else the server's."""
    keep = row.get("local_keep_days")
    quota = row.get("local_quota_gb")
    return (settings.local_keep_days if keep is None else keep,
            settings.local_quota_gb if quota is None else quota)


def local_storages(settings, db, vault, resolver, user):
    """Every LocalStorage that holds this person's videos: the current destination and the earlier ones."""
    destinations = [resolve(settings, db, vault, user["id"])] + [old for _, old in remembered(db, vault, user["id"])]
    found, seen = [], set()
    for resolved in destinations:
        if resolved.kind != "local":
            continue
        for camera in db.cameras_for_user(user["id"]):
            ctx = SimpleNamespace(data_dir=camera_data_dir(settings, camera), playlist_prefix=PREFIX)
            try:
                storage = build(resolved, settings, ctx, resolver)
            except StorageError:
                continue
            if isinstance(storage, LocalStorage) and str(storage.root) not in seen:
                seen.add(str(storage.root))
                found.append(storage)
    return found


def _free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _remove(storage, item):
    """Delete one video; returns the bytes freed (0 when it could not be deleted)."""
    try:
        return storage.remove(item.day, item.name)
    except (OSError, StorageError) as err:
        log.warning("could not delete %s: %s", item.name, err)
        return 0


def _oldest_first(storages):
    return sorted(((item, storage) for storage in storages for item in storage.videos()),
                  key=lambda pair: (pair[0].day, pair[0].name))


def run_local_retention(settings, db, vault, resolver, now, free_bytes=_free_bytes):
    """Apply the three rules once. Returns the state the pages use for their warnings:
    {"disk_low": bool, "free_gb": float | None, "users": {user id: {"used", "limit", "full", "trimmed", "keep_days"}}}."""
    state = {"disk_low": False, "free_gb": None, "users": {}}
    everyone = []
    for user in db.list_users():
        storages = local_storages(settings, db, vault, resolver, user)
        if not storages:
            continue
        everyone += storages
        keep_days, quota_gb = effective_limits(settings, db.user_settings(user["id"]))
        if keep_days > 0:
            cutoff = (now.date() - timedelta(days=keep_days)).isoformat()
            for item, storage in _oldest_first(storages):
                if item.day >= cutoff:
                    break
                _remove(storage, item)
        trimmed = 0       # videos deleted because the person's space was full (not because they got old)
        limit = int(quota_gb * GB)
        used = sum(storage.usage() for storage in storages)
        if limit > 0:
            for item, storage in _oldest_first(storages):
                if used <= limit:
                    break
                freed = _remove(storage, item)
                used -= freed
                trimmed += 1 if freed else 0
        full = bool(limit > 0 and (trimmed > 0 or used >= NEARLY_FULL * limit))
        state["users"][user["id"]] = {"used": used, "limit": limit, "full": full, "trimmed": trimmed, "keep_days": keep_days}
    minimum = int(settings.disk_min_free_gb * GB)
    free = free_bytes(settings.data_dir) if minimum > 0 else None
    if free is not None:
        state["free_gb"] = round(free / GB, 2)
    if minimum > 0 and free is not None and free < minimum:
        state["disk_low"] = True
        for item, storage in _oldest_first(everyone):
            if free >= minimum:
                break
            _remove(storage, item)
            free = free_bytes(settings.data_dir)
            if free is None:
                break
        state["free_gb"] = None if free is None else round(free / GB, 2)
    return state
