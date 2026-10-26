"""Runs everything one camera needs, on its own threads: the same four loops `tapo_motion.py run` uses.

`on_status(kind, detail)` is called with kind "online", "offline", "motion", "uploaded" or "error"; the
single-user program ignores it, the multi-account supervisor stores it for the personal page.
"""
import queue
import threading

import tapo_motion
import tapo_uploader as base


class CameraWorker:
    def __init__(self, ctx, on_status):
        self.ctx = ctx
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.threads = []

    def _report(self, kind, detail=""):
        try:
            self.on_status(kind, self.ctx.scrub(detail))
        except Exception:  # a broken status sink must never stop the recording
            base.log.exception("status callback failed")

    def start(self):
        ctx, stop, report = self.ctx, self.stop_event, self._report
        events = queue.Queue()
        # looked up at start time so a test can swap a loop for a stub
        targets = [
            (base.record_loop, (ctx, ctx.buffer_dir, ctx.segment, stop)),
            (tapo_motion.onvif_loop, (ctx, events, stop, report)),
            (tapo_motion.controller, (ctx, events, stop, report)),
            (tapo_motion.daily_loop, (ctx, stop, report)),
        ]
        self.threads = [threading.Thread(target=self._guard(target), args=args, daemon=True,
                                         name=f"{ctx.name}-{target.__name__}")
                        for target, args in targets]
        for thread in self.threads:
            thread.start()

    def _guard(self, target):
        def run(*args):
            try:
                target(*args)
            except Exception as err:
                base.log.exception("%s stopped", target.__name__)
                self._report("error", f"{target.__name__} stopped: {err}")
        return run

    @property
    def alive(self):
        return any(thread.is_alive() for thread in self.threads)

    def stop(self, timeout=10):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout)
