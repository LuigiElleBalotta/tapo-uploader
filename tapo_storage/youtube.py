"""YouTube as a destination: a thin wrapper around the existing upload code in `tapo_uploader`."""
from datetime import datetime

from .base import Storage, StorageError, StoredItem


class YouTubeStorage(Storage):
    kind = "youtube"
    uses_quota = True            # videos.insert costs 1600 of the project's 10,000 daily units
    browsable = False            # the personal page lists the channel's playlists itself
    needs_browser_audio = False  # YouTube transcodes whatever it gets

    def __init__(self, ctx, state=None):
        self.ctx, self.state, self._client = ctx, state, None

    def _youtube(self):
        import tapo_uploader as base
        if self._client is None:
            self._client = base.youtube_client(self.ctx)
        return self._client

    def connect(self):
        self._youtube()

    def check(self):
        try:
            self._youtube()
        except Exception as err:
            raise StorageError(self.ctx.scrub(str(err)) or "YouTube is not linked") from err

    def put(self, path, day, title, description=""):
        import tapo_uploader as base
        if self.state is None:
            self.state = base.load_state(self.ctx)
        base.upload(self.ctx, self._youtube(), self.state, path, datetime.strptime(day, "%Y-%m-%d"),
                    title=title, description=description)
        return StoredItem(title, day, None, "", None)
