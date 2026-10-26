"""Every destination a user has used stays a source: switching to another one must not hide the old videos.

A source keeps the settings the destination had (encrypted, like the active ones), so its videos can still be listed
and played. Removing a source from the list never deletes a video.
"""
import json
from urllib.parse import urlsplit

from .crypto import VaultError
from .storage import Resolved


def label_for(resolved):
    """What the user sees on a video's badge: where it lives."""
    kind = resolved.kind
    if kind == "youtube":
        return "YouTube"
    if kind == "onedrive":
        return "OneDrive"
    if kind == "webdav":
        host = urlsplit(resolved.config.get("url", "")).hostname
        return f"WebDAV · {host}" if host else "WebDAV"
    if kind == "local":
        return "Server folder"
    if kind == "rclone":
        return "Cloud storage (rclone)"
    return kind


def remember(db, vault, user_id, resolved):
    """Keep `resolved` in the user's list of sources (once per distinct destination). Returns the source id."""
    config_enc = vault.encrypt(json.dumps(resolved.config)) if resolved.config else None
    return db.add_storage_source(user_id, resolved.signature(), resolved.kind, resolved.source, config_enc,
                                 label_for(resolved))


def remembered(db, vault, user_id):
    """[(source id, Resolved)] for the sources that can still be read; one that cannot is left out, not fatal."""
    found = []
    for row in db.storage_sources(user_id):
        try:
            config = json.loads(vault.decrypt(row["config_enc"])) if row["config_enc"] else {}
            if not isinstance(config, dict):
                raise ValueError("not an object")
        except (VaultError, ValueError, TypeError, AttributeError):
            continue
        found.append((row["id"], Resolved(row["kind"], config, row["origin"])))
    return found


def source_resolved(db, vault, user_id, source_id):
    """The remembered destination with this id, or None (unknown, somebody else's, or unreadable)."""
    for found_id, resolved in remembered(db, vault, user_id):
        if found_id == source_id:
            return resolved
    return None
