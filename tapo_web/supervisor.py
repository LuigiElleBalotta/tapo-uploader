"""Runs one worker per enabled camera of an active user, keeps their status in the database and cleans up.

`sync()` makes the running workers match the database (start, stop, restart after a change or a crash with a
growing delay). A camera's address is resolved and checked every time its worker is (re)started, and the
worker connects to that checked IP. `maintenance()` applies the retention rules.
"""
import dataclasses
import logging
import shutil
import socket
import threading
import time
from datetime import datetime

from tapo_storage.base import StorageError

from .cameras import camera_data_dir, safe_context
from .crypto import VaultError
from .quota import QuotaLedger, UploadGate, stagger_slots
from .security import UnsafeHost
from .storage import build, resolve
from .youtube_link import LinkRevoked, youtube_for_user

log = logging.getLogger("tapo.web")
SYNC_SECONDS = 30
MAINTENANCE_SECONDS = 3600
STALE_CLIP_DAYS = 7
EVENT_DAYS = 30


def _signature(camera):
    return (camera["user_id"], camera["host"], camera["port"], camera["onvif_port"], camera["stream"],
            camera["username_enc"], camera["password_enc"])


def count_pending_clips(events_dir):
    """Clips waiting for the daily upload (the joined day.mp4 is not one of them)."""
    return sum(1 for day in events_dir.glob("[0-9]" * 8) for _ in day.glob("[0-9]*.mp4")) if events_dir.exists() else 0


def purge_inactive(db, data_dir, now, retention_days):
    """Delete the data and the tokens of users deactivated for more than `retention_days`. Returns how many."""
    cutoff = now.timestamp() - retention_days * 86400
    purged = 0
    for user in db.list_users():
        if not user["active"] and user["deactivated_at"] is not None and user["deactivated_at"] < cutoff:
            shutil.rmtree(data_dir / "users" / str(user["id"]), ignore_errors=True)
            db.delete_user(user["id"])  # cascades to cameras, events and the stored YouTube token
            purged += 1
    return purged


def discard_stale_clips(events_dir, now, max_age_days=STALE_CLIP_DAYS):
    """Delete clips older than `max_age_days` (used when nothing can upload them). Returns how many files went."""
    if not events_dir.exists():
        return 0
    limit = now.timestamp() - max_age_days * 86400
    removed = 0
    for day in events_dir.glob("[0-9]" * 8):
        for clip in day.glob("*.mp4"):
            if clip.stat().st_mtime < limit:
                clip.unlink(missing_ok=True)
                removed += 1
        try:
            day.rmdir()
        except OSError:
            pass  # still has newer clips
    return removed


class Supervisor:
    def __init__(self, db, vault, settings, worker_factory=None, clock=time.time, resolver=socket.getaddrinfo):
        if worker_factory is None:
            from camera_worker import CameraWorker
            worker_factory = CameraWorker
        self.db, self.vault, self.settings = db, vault, settings
        self.worker_factory, self.clock, self.resolver = worker_factory, clock, resolver
        self.ledger = QuotaLedger(db, settings.quota_daily)
        self.running = {}   # camera id -> {"worker", "signature"}
        self.backoff = {}   # camera id -> {"failures", "not_before"}
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    # ----- workers -----

    def _youtube_factory(self, user_id):
        def factory():
            client = youtube_for_user(self.db, self.vault, self.settings, user_id)
            if client is None:
                raise LinkRevoked("YouTube is not linked")
            return client
        return factory

    def _start(self, camera, slot, resolved):
        cid = camera["id"]
        try:
            ctx = safe_context(camera, self.vault, self.settings, youtube=self._youtube_factory(camera["user_id"]),
                               resolver=self.resolver)
        except UnsafeHost as err:
            self._problem(camera, "blocked", f"That address is not allowed: {err}.")
            return
        except VaultError:
            self._problem(camera, "error", "The stored camera credentials cannot be read.")
            return
        try:
            storage = build(resolved, self.settings, ctx, self.resolver)
        except StorageError as err:
            self._problem(camera, "error", f"The recordings destination cannot be used: {err}")
            return
        ctx = dataclasses.replace(ctx, upload_at=slot, storage=storage)  # each camera uploads in its own time slot
        ctx.ensure_dirs()
        import tapo_motion
        for _, path in tapo_motion.segments(ctx):
            path.unlink(missing_ok=True)  # leftovers of a previous run
        gate = UploadGate(self.ledger, cid, slot) if resolved.kind == "youtube" else None  # only YouTube has a quota
        worker = self.worker_factory(ctx, lambda kind, detail="", cid=cid: self._on_status(cid, kind, detail), gate=gate)
        worker.start()
        self.running[cid] = {"worker": worker, "signature": _signature(camera), "storage": resolved.signature()}
        log.info("camera %s: worker started", cid)

    def _problem(self, camera, status, message):
        fresh = camera["last_error"] != message
        self.db.set_camera_status(camera["id"], status=status, last_error=message)
        if fresh:
            self.db.add_event(camera["id"], "error", message, ts=self.clock())

    def sync(self):
        wanted = {camera["id"]: camera for camera in self.db.all_enabled_cameras()}
        resolved = {cid: resolve(self.settings, self.db, self.vault, camera["user_id"]) for cid, camera in wanted.items()}
        with self.lock:
            for cid in list(self.running):
                entry, camera = self.running[cid], wanted.get(cid)
                if camera is None or _signature(camera) != entry["signature"] or resolved[cid].signature() != entry["storage"]:
                    entry["worker"].stop()
                    del self.running[cid]
                    self.backoff.pop(cid, None)
                    if camera is None:
                        self._mark_stopped(cid)
                elif not entry["worker"].alive:  # every thread of the worker died
                    del self.running[cid]
                    failures = self.backoff.get(cid, {"failures": 0})["failures"] + 1
                    self.backoff[cid] = {"failures": failures,
                                         "not_before": self.clock() + min(300, 5 * 2 ** failures)}
                    log.warning("camera %s: worker died, restart in %ss", cid, min(300, 5 * 2 ** failures))
            # the quota is YouTube's: only its cameras take turns, the others upload at the base time
            slots = stagger_slots([cid for cid in wanted if resolved[cid].kind == "youtube"], self.settings.upload_at,
                                  self.settings.upload_stagger_minutes)
            for cid, camera in wanted.items():
                if cid in self.running:
                    continue
                wait = self.backoff.get(cid)
                if wait and self.clock() < wait["not_before"]:
                    continue
                self._start(camera, slots.get(cid, self.settings.upload_at), resolved[cid])

    def _mark_stopped(self, camera_id):
        if self.db.camera_by_id(camera_id):
            self.db.set_camera_status(camera_id, status="stopped")

    def stop_all(self):
        self._stop.set()
        with self.lock:
            for entry in self.running.values():
                entry["worker"].stop()
            self.running.clear()

    # ----- status -----

    def _on_status(self, camera_id, kind, detail):
        camera = self.db.camera_by_id(camera_id)
        if camera is None:
            return
        now, detail = self.clock(), (detail or "")[:300]
        if kind == "online":
            fields = {"status": "online", "last_seen": now}
            if camera["status"] == "offline":   # the connection is back: its message is history (an upload problem stays)
                fields["last_error"] = None
            self.db.set_camera_status(camera_id, **fields)
        elif kind == "motion":
            self.db.set_camera_status(camera_id, last_motion=now)
            self.db.add_event(camera_id, "motion", detail, ts=now)
        elif kind == "uploaded":
            self.db.set_camera_status(camera_id, last_upload=now, last_error=None)
            self.db.add_event(camera_id, "uploaded", detail, ts=now)
        elif kind in ("offline", "error"):
            if camera["last_error"] != detail:  # the same problem every minute is not news
                self.db.add_event(camera_id, kind, detail, ts=now)
            fields = {"last_error": detail}
            if kind == "offline":
                fields["status"] = "offline"
            self.db.set_camera_status(camera_id, **fields)

    def status(self, camera_id):
        camera = self.db.camera_by_id(camera_id)
        entry = self.running.get(camera_id)
        pending = 0
        if camera:
            pending = count_pending_clips(camera_data_dir(self.settings, camera) / "events")
        return {"running": bool(entry and entry["worker"].alive), "pending_clips": pending}

    # ----- retention -----

    def maintenance(self, now=None):
        now = now or datetime.fromtimestamp(self.clock())
        purge_inactive(self.db, self.settings.data_dir, now, self.settings.retention_days)
        self.db.purge_events(EVENT_DAYS)
        for user in self.db.list_users():
            if (resolve(self.settings, self.db, self.vault, user["id"]).kind == "youtube"
                    and self.db.youtube_link(user["id"]) is None):  # nothing can upload their clips: do not fill the disk
                for camera in self.db.cameras_for_user(user["id"]):
                    discard_stale_clips(camera_data_dir(self.settings, camera) / "events", now, STALE_CLIP_DAYS)

    # ----- background loop -----

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        self._thread.start()

    def _loop(self):
        last_maintenance = 0.0
        while not self._stop.is_set():
            try:
                self.sync()
                if self.clock() - last_maintenance >= MAINTENANCE_SECONDS:
                    self.maintenance()
                    last_maintenance = self.clock()
            except Exception:
                log.exception("supervisor round failed")
            self._stop.wait(SYNC_SECONDS)
