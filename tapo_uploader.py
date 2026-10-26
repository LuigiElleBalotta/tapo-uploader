"""Record a Tapo camera RTSP stream in fixed segments and upload each to YouTube.

`run` starts two threads. One keeps ffmpeg recording clock-aligned .mp4 segments into
RECORD_DIR (restarting it with backoff if the stream drops). The other uploads every
finished segment as a private video, adds it to the playlist of its day and deletes the
file. See README.md for configuration and deployment.

Commands:
    python tapo_uploader.py auth     one-time OAuth login (prints a URL to open in a browser)
    python tapo_uploader.py check    probe the RTSP stream for a few seconds
    python tapo_uploader.py test     record 60 s and upload them: checks everything end to end
    python tapo_uploader.py run      record + upload forever
"""
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
# where tokens, state and recordings live: a persistent volume in a container, the project folder otherwise
DATA = Path(os.getenv("DATA_DIR") or BASE)
DATA.mkdir(parents=True, exist_ok=True)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
FILE_RE = re.compile(r"^(\d{8})_(\d{6})\.mp4$")
STATE_FILE = DATA / "state.json"

log = logging.getLogger("tapo")


def env(name, default=None):
    value = os.getenv(name, default)
    if value is None:
        sys.exit(f"Missing {name} in .env")
    return value


def camera_credentials():
    return f"{quote(env('CAMERA_USER'), safe='')}:{quote(env('CAMERA_PASS'), safe='')}"


def rtsp_url():
    host, port, stream = env("CAMERA_HOST"), env("CAMERA_PORT", "554"), env("CAMERA_STREAM", "stream1")
    return f"rtsp://{camera_credentials()}@{host}:{port}/{stream}"


def scrub(text):
    """ffmpeg and ffprobe echo the full rtsp:// URL in their errors: never log the password."""
    return text.replace(camera_credentials(), "***")


def run_scrubbed(cmd, timeout=None):
    """Run a command and print its output with the camera credentials hidden."""
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    print(scrub(result.stdout + result.stderr), end="")
    return result.returncode


def record_dir():
    path = DATA / env("RECORD_DIR", "recordings")
    path.mkdir(exist_ok=True)
    return path


# ---------- recording ----------

def ffmpeg_cmd(out_dir, segment_seconds):
    return [
        # "warning" would log a timestamp complaint every second; real failures are errors
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        # the camera sends H.264 packets without usable timestamps
        "-use_wallclock_as_timestamps", "1", "-fflags", "+genpts",
        "-rtsp_transport", "tcp", "-i", rtsp_url(),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-ar", "16000", "-ac", "1",
        "-f", "segment", "-segment_time", str(segment_seconds),
        "-segment_atclocktime", "1", "-reset_timestamps", "1",
        # fragmented mp4 stays playable if ffmpeg is killed (restart, reboot): a plain mp4
        # without its final moov atom is unreadable
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        "-strftime", "1",
        str(out_dir / "%Y%m%d_%H%M%S.mp4"),
    ]


def pump_stderr(proc):
    for line in proc.stderr:
        log.warning("ffmpeg: %s", scrub(line.rstrip()))


def record_loop(out_dir, segment_seconds, stop):
    backoff = 5
    while not stop.is_set():
        started = time.monotonic()
        log.info("ffmpeg start")
        proc = subprocess.Popen(ffmpeg_cmd(out_dir, segment_seconds),
                                stderr=subprocess.PIPE, text=True, errors="replace")
        threading.Thread(target=pump_stderr, args=(proc,), daemon=True).start()
        while proc.poll() is None and not stop.is_set():
            time.sleep(2)
        if stop.is_set():
            proc.terminate()
            return
        # a long-lived run means the failure was transient, so reset backoff
        backoff = 5 if time.monotonic() - started > 300 else min(backoff * 2, 300)
        log.warning("ffmpeg exited (%s), retry in %ss", proc.returncode, backoff)
        stop.wait(backoff)


# ---------- youtube ----------

def secret_file(file_name, env_var):
    """Path of a secret file. Containers have no files to copy: if it is missing and the
    environment variable holds its content, the file is created from it."""
    path = DATA / file_name
    content = os.getenv(env_var)
    if content and not path.exists():
        path.write_text(content)
        path.chmod(0o600)
    return path


def youtube_client():
    token_path = secret_file(env("YOUTUBE_TOKEN", "token.json"), "YOUTUBE_TOKEN_JSON")
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                secret_file(env("YOUTUBE_CLIENT_SECRET", "client_secret.json"), "YOUTUBE_CLIENT_SECRET_JSON"),
                SCOPES)
            # print the URL instead of opening the default browser: the Google account may live in another one
            creds = flow.run_local_server(port=0, open_browser=False)
        token_path.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"playlists": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def playlist_for(yt, state, day):
    title = f"{env('PLAYLIST_PREFIX', 'Tapo')} {day}"
    cached = state["playlists"].get(day)
    if cached:
        return cached
    token = None
    while True:
        resp = yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=token).execute()
        for item in resp["items"]:
            if item["snippet"]["title"] == title:
                state["playlists"][day] = item["id"]
                save_state(state)
                return item["id"]
        token = resp.get("nextPageToken")
        if not token:
            break
    body = {
        "snippet": {"title": title},
        "status": {"privacyStatus": env("YOUTUBE_PRIVACY", "private")},
    }
    pid = yt.playlists().insert(part="snippet,status", body=body).execute()["id"]
    state["playlists"][day] = pid
    save_state(state)
    return pid


def upload(yt, state, path, start, title=None, description=""):
    if title is None:
        # the real end is the last write: partial segments (restart, stream drop) are shorter
        end = datetime.fromtimestamp(path.stat().st_mtime)
        title = f"{env('PLAYLIST_PREFIX', 'Tapo')} {start:%Y-%m-%d %H:%M}-{end:%H:%M}"
    body = {
        "snippet": {"title": title, "description": description, "categoryId": "22"},
        "status": {"privacyStatus": env("YOUTUBE_PRIVACY", "private"), "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response["id"]
    playlist_id = playlist_for(yt, state, f"{start:%Y-%m-%d}")
    yt.playlistItems().insert(part="snippet", body={"snippet": {
        "playlistId": playlist_id,
        "resourceId": {"kind": "youtube#video", "videoId": video_id},
    }}).execute()
    log.info("uploaded %s -> %s", path.name, video_id)


def pending_files(out_dir):
    """Completed segments only: ffmpeg is still writing any file touched recently."""
    now = time.time()
    for path in sorted(out_dir.glob("*.mp4")):
        match = FILE_RE.match(path.name)
        if match and now - path.stat().st_mtime > 120:
            yield path, datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")


def is_quota_error(err):
    return err.resp.status == 403 and b"quotaExceeded" in err.content


def upload_loop(out_dir, stop):
    state = load_state()
    yt = None
    while not stop.is_set():
        try:
            for path, start in pending_files(out_dir):
                yt = yt or youtube_client()
                upload(yt, state, path, start)
                path.unlink()
        except HttpError as err:
            if is_quota_error(err):
                log.error("YouTube quota exceeded, sleeping 1h")
                stop.wait(3600)
                continue
            log.exception("YouTube error")
        except Exception:
            log.exception("upload failed")
            yt = None
        stop.wait(60)


# ---------- commands ----------

def cmd_check():
    cmd = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-show_entries",
           "stream=codec_type,codec_name,width,height", "-of", "default=nw=1", rtsp_url()]
    sys.exit(run_scrubbed(cmd, timeout=30))


def cmd_test():
    """Same ffmpeg command and upload path as `run`, but only 60 s and in a temp dir."""
    yt = youtube_client()
    state = load_state()
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ffmpeg_cmd(Path(tmp), 21600)
        cmd[-1:-1] = ["-t", "60"]  # stop after 60 s, before the output pattern
        if run_scrubbed(cmd, timeout=180):
            sys.exit("ffmpeg failed, see the output above")
        for path in sorted(Path(tmp).glob("*.mp4")):
            match = FILE_RE.match(path.name)
            start = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
            upload(yt, state, path, start)


def cmd_run():
    segment_seconds = int(env("SEGMENT_SECONDS", "21600"))
    out_dir = record_dir()
    youtube_client()  # fail fast if not authorised
    stop = threading.Event()
    threads = [
        threading.Thread(target=record_loop, args=(out_dir, segment_seconds, stop)),
        threading.Thread(target=upload_loop, args=(out_dir, stop)),
    ]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping")
        stop.set()
        for thread in threads:
            thread.join()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    commands = {"auth": youtube_client, "check": cmd_check, "test": cmd_test, "run": cmd_run}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
