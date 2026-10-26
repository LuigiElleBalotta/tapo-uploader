"""Everything one camera needs, passed explicitly instead of read from the environment.

Single-user mode builds it from `.env` (`CameraContext.from_env`); the multi-account web app builds
one per camera from the database. The recording, ONVIF and upload code only ever sees this object.
"""
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

BASE = Path(__file__).resolve().parent


def _env(name, default=None):
    value = os.getenv(name, default)
    if value is None:
        sys.exit(f"Missing {name} in .env")
    return value


@dataclass(frozen=True)
class CameraContext:
    name: str
    host: str
    port: int = 554
    user: str = ""
    password: str = ""
    stream: str = "stream1"
    # ONVIF can sit behind a different host/port than RTSP (e.g. a second port forward); "" means host
    onvif_host: str = ""
    onvif_port: int = 2020
    data_dir: Path = Path(".")
    pre: int = 30
    post: int = 300
    extend_within: int = 30
    segment: int = 10
    segment_seconds: int = 21600
    upload_at: time = time(21, 0)
    # ONVIF items that count as "something moves": IsMotion = motion detection, IsPeople = person detection
    motion_events: frozenset = frozenset({"ismotion", "ispeople"})
    playlist_prefix: str = "Tapo"
    privacy: str = "private"
    record_name: str = "recordings"
    buffer_name: str = "buffer"
    events_name: str = "events"
    # returns an authorised YouTube client; None means the single-user token/client files are used
    youtube: Optional[Callable] = None

    def credentials(self):
        return f"{quote(self.user, safe='')}:{quote(self.password, safe='')}"

    def rtsp_url(self):
        return f"rtsp://{self.credentials()}@{self.host}:{self.port}/{self.stream}"

    def scrub(self, text):
        """ffmpeg and ffprobe echo the full rtsp:// URL in their errors: never log the password."""
        return text.replace(self.credentials(), "***")

    @property
    def onvif_address(self):
        return self.onvif_host or self.host

    @property
    def record_dir(self):
        return self.data_dir / self.record_name

    @property
    def buffer_dir(self):
        return self.data_dir / self.buffer_name

    @property
    def events_dir(self):
        return self.data_dir / self.events_name

    @property
    def state_file(self):
        return self.data_dir / "state.json"

    @classmethod
    def from_env(cls):
        host = _env("CAMERA_HOST")
        return cls(
            name="camera",
            host=host,
            port=int(_env("CAMERA_PORT", "554")),
            user=_env("CAMERA_USER"),
            password=_env("CAMERA_PASS"),
            stream=_env("CAMERA_STREAM", "stream1"),
            onvif_host=_env("ONVIF_HOST", host),
            onvif_port=int(_env("ONVIF_PORT", "2020")),
            data_dir=Path(os.getenv("DATA_DIR") or BASE),
            pre=int(_env("MOTION_PRE_SECONDS", "30")),
            post=int(_env("MOTION_POST_SECONDS", "300")),
            extend_within=int(_env("MOTION_EXTEND_WITHIN", "30")),
            segment=int(_env("BUFFER_SEGMENT_SECONDS", "10")),
            segment_seconds=int(_env("SEGMENT_SECONDS", "21600")),
            upload_at=datetime.strptime(_env("UPLOAD_AT", "21:00"), "%H:%M").time(),
            motion_events=frozenset(name.strip().lower()
                                    for name in _env("MOTION_EVENTS", "IsMotion,IsPeople").split(",")),
            playlist_prefix=_env("PLAYLIST_PREFIX", "Tapo"),
            privacy=_env("YOUTUBE_PRIVACY", "private"),
            record_name=_env("RECORD_DIR", "recordings"),
            buffer_name=_env("BUFFER_DIR", "buffer"),
            events_name=_env("EVENTS_DIR", "events"),
        )
