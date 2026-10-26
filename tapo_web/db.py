"""SQLite storage for the multi-account mode.

Secrets are stored only in `*_enc` columns: the callers encrypt with `Vault` before they get here.
Every read of a user's data takes the user id, so one user can never be handed another's rows.
"""
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    password_hash TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    deactivated_at REAL,
    created_at REAL NOT NULL,
    UNIQUE (origin, username)
);
CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    onvif_port INTEGER NOT NULL,
    stream TEXT NOT NULL,
    username_enc TEXT NOT NULL,
    password_enc TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    last_seen REAL,
    last_motion REAL,
    last_upload REAL,
    last_error TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS youtube_links (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    token_enc TEXT NOT NULL,
    channel_title TEXT,
    client_source TEXT NOT NULL,
    client_secret_enc TEXT,
    linked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS quota (
    day TEXT PRIMARY KEY,
    units INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""

# the only camera columns set_camera_status may write: it never builds SQL from caller-supplied names
CAMERA_FIELDS = {"name", "host", "port", "onvif_port", "stream", "username_enc", "password_enc", "enabled",
                 "status", "last_seen", "last_motion", "last_upload", "last_error"}


class Database:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()

    # ----- plumbing -----

    def _run(self, sql, params=()):
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _one(self, sql, params=()):
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql, params=()):
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def migrate(self):
        with self._lock:
            self._conn.executescript(SCHEMA)
            if self._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                self._conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            self._conn.commit()

    # ----- users -----

    def add_user(self, origin, username, display_name, role, password_hash=None):
        cursor = self._run("INSERT INTO users (origin, username, display_name, role, password_hash, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (origin, username, display_name, role, password_hash, time.time()))
        return cursor.lastrowid

    def user_by_username(self, origin, username):
        return self._one("SELECT * FROM users WHERE origin = ? AND username = ?", (origin, username))

    def user_by_id(self, user_id):
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def list_users(self):
        return self._all("SELECT * FROM users ORDER BY id")

    def set_user_active(self, user_id, active):
        self._run("UPDATE users SET active = ?, deactivated_at = ? WHERE id = ?",
                  (1 if active else 0, None if active else time.time(), user_id))

    def set_user_role(self, user_id, role):
        self._run("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    def update_user_profile(self, user_id, display_name, role):
        self._run("UPDATE users SET display_name = ?, role = ? WHERE id = ?", (display_name, role, user_id))

    def delete_user(self, user_id):
        self._run("DELETE FROM users WHERE id = ?", (user_id,))

    # ----- cameras -----

    def add_camera(self, user_id, name, host, port, onvif_port, stream, username_enc, password_enc):
        cursor = self._run(
            "INSERT INTO cameras (user_id, name, host, port, onvif_port, stream, username_enc, password_enc, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, host, port, onvif_port, stream, username_enc, password_enc, time.time()))
        return cursor.lastrowid

    def camera_for_user(self, user_id, camera_id):
        return self._one("SELECT * FROM cameras WHERE id = ? AND user_id = ?", (camera_id, user_id))

    def cameras_for_user(self, user_id):
        return self._all("SELECT * FROM cameras WHERE user_id = ? ORDER BY id", (user_id,))

    def all_enabled_cameras(self):
        return self._all("SELECT cameras.* FROM cameras JOIN users ON users.id = cameras.user_id "
                         "WHERE cameras.enabled = 1 AND users.active = 1 ORDER BY cameras.id")

    def set_camera_status(self, camera_id, **fields):
        unknown = set(fields) - CAMERA_FIELDS
        if unknown or not fields:
            raise ValueError(f"cannot set camera fields: {sorted(unknown) or 'none given'}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._run(f"UPDATE cameras SET {assignments} WHERE id = ?", (*fields.values(), camera_id))

    def delete_camera(self, user_id, camera_id):
        return self._run("DELETE FROM cameras WHERE id = ? AND user_id = ?", (camera_id, user_id)).rowcount > 0

    # ----- YouTube -----

    def save_youtube_link(self, user_id, token_enc, channel_title, client_source, client_secret_enc=None):
        self._run("INSERT INTO youtube_links (user_id, token_enc, channel_title, client_source, client_secret_enc, "
                  "linked_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET token_enc = excluded.token_enc, "
                  "channel_title = excluded.channel_title, client_source = excluded.client_source, "
                  "client_secret_enc = excluded.client_secret_enc, linked_at = excluded.linked_at",
                  (user_id, token_enc, channel_title, client_source, client_secret_enc, time.time()))

    def update_youtube_token(self, user_id, token_enc):
        self._run("UPDATE youtube_links SET token_enc = ? WHERE user_id = ?", (token_enc, user_id))

    def youtube_link(self, user_id):
        return self._one("SELECT * FROM youtube_links WHERE user_id = ?", (user_id,))

    def delete_youtube_link(self, user_id):
        self._run("DELETE FROM youtube_links WHERE user_id = ?", (user_id,))

    # ----- quota and events -----

    def add_quota_use(self, day, units):
        self._run("INSERT INTO quota (day, units) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET units = units + excluded.units",
                  (day, units))

    def quota_used(self, day):
        row = self._one("SELECT units FROM quota WHERE day = ?", (day,))
        return row["units"] if row else 0

    def add_event(self, camera_id, kind, detail, ts=None):
        self._run("INSERT INTO events (camera_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                  (camera_id, ts if ts is not None else time.time(), kind, detail))

    def events_for_camera(self, camera_id, limit=50):
        return self._all("SELECT * FROM events WHERE camera_id = ? ORDER BY ts DESC LIMIT ?", (camera_id, limit))

    def purge_events(self, older_than_days):
        self._run("DELETE FROM events WHERE ts < ?", (time.time() - older_than_days * 86400,))
