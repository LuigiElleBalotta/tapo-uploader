"""The YouTube API quota the shared Google project spends, and the gate that keeps uploads inside it.

The project has one daily budget (10,000 units by default) for every user. One day's upload of one camera costs
a video insert (1600), a playlist item (50), looking for the day's playlist (1) and creating it (50). The ledger
counts what was spent in the current Pacific day (the quota resets at midnight Pacific Time); the gate lets a
camera upload only after its own time slot and only if a full upload still fits. Naive datetimes mean the
server's local time, as everywhere else in the program.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

UPLOAD_COST = 1600
PLAYLIST_ITEM_COST = 50
PLAYLIST_INSERT_COST = 50
LIST_COST = 1
DAY_UPLOAD_COST = UPLOAD_COST + PLAYLIST_ITEM_COST + PLAYLIST_INSERT_COST + LIST_COST

PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_day(now):
    """The quota day (YYYY-MM-DD, Pacific Time) a moment belongs to."""
    return now.astimezone(PACIFIC).strftime("%Y-%m-%d")


class QuotaLedger:
    def __init__(self, db, daily_limit):
        self.db, self.daily_limit = db, daily_limit

    def record(self, units, now=None):
        self.db.add_quota_use(pacific_day(now or datetime.now()), units)

    def used(self, now=None):
        return self.db.quota_used(pacific_day(now or datetime.now()))

    def can_afford(self, units, now=None):
        return self.used(now) + units <= self.daily_limit

    def exhaust(self, now=None):
        """Google itself said the quota is gone: nothing more fits today, whatever our own count says."""
        used = self.used(now)
        if used < self.daily_limit:
            self.record(self.daily_limit - used, now)


def stagger_slots(camera_ids, upload_at, stagger_minutes):
    """Spread the daily uploads: the lowest camera id uploads at `upload_at`, the next one `stagger_minutes` later..."""
    start = datetime.combine(datetime(2000, 1, 1), upload_at)
    return {camera_id: (start + timedelta(minutes=index * stagger_minutes)).time()
            for index, camera_id in enumerate(sorted(camera_ids))}


class UploadGate:
    def __init__(self, ledger, camera_id, slot: time):
        self.ledger, self.camera_id, self.slot = ledger, camera_id, slot

    def allowed(self, now=None):
        now = now or datetime.now()
        return now.time() >= self.slot and self.ledger.can_afford(DAY_UPLOAD_COST, now)

    def charge(self, now=None):
        self.ledger.record(DAY_UPLOAD_COST, now)

    def exhausted(self, now=None):
        self.ledger.exhaust(now)
