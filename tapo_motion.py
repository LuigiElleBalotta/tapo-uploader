"""Record the Tapo camera only around motion and upload one video per day.

How it works (see README.md):
 - ffmpeg keeps recording the stream in short segments into BUFFER_DIR, but only the last
   minute or so is kept (a ring buffer), so nothing piles up while nothing happens.
 - The camera's own motion detection is read over ONVIF (PullPoint events).
 - The first motion starts an event: the clip goes from MOTION_PRE_SECONDS before it to
   MOTION_POST_SECONDS after it. Motion in the last MOTION_EXTEND_WITHIN seconds of the
   window extends it by another MOTION_POST_SECONDS; earlier motion is ignored.
 - Each event becomes a clip in EVENTS_DIR/AAAAMMGG/. From UPLOAD_AT every clip of the day is
   joined into one video and uploaded as private (the description lists when each event
   starts, which YouTube shows as chapters), then the files are deleted.

Every function takes the camera's CameraContext as its first argument, so the same code serves
one camera configured in .env or many cameras configured in a database.

Commands:
    python tapo_motion.py listen [seconds]  print the motion events sent by the camera
    python tapo_motion.py run               record around motion, upload once a day
    python tapo_motion.py upload            join and upload the pending clips now
"""
import base64
import hashlib
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from googleapiclient.errors import HttpError

import tapo_uploader as base
from camera_context import CameraContext
from tapo_storage.base import StorageError
from tapo_uploader import FILE_RE, log

# the camera's subscription is short-lived: ask for a new one before it expires
SUBSCRIPTION_SECONDS = 90

WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
BASE64_ENCODING = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-soap-message-security-1.0#Base64Binary"


def _ignore(kind, detail=""):
    """Default status sink: the single-user programs only log."""


# ---------- ONVIF motion events ----------

def soap_envelope(ctx, body):
    """SOAP message with a WS-Security UsernameToken (password digest), as ONVIF requires."""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + ctx.password.encode()).digest()).decode()
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Header>'
        f'<wsse:Security xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}"><wsse:UsernameToken>'
        f'<wsse:Username>{ctx.user}</wsse:Username>'
        f'<wsse:Password Type="{PASSWORD_DIGEST}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{BASE64_ENCODING}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
        f'<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>'
        f'<s:Body>{body}</s:Body></s:Envelope>'
    ).encode()


def soap(ctx, url, body, timeout=30):
    """POST a SOAP message over a plain socket.

    http.client and urllib hang on this camera: it does not close the connection or give a usable
    Content-Length for every reply, so we read until the end of the SOAP envelope instead.
    """
    host, path = re.match(r"https?://([^/]+)(/.*)", url).groups()
    if host.startswith("["):  # an IPv6 literal: [address]:port
        name, _, rest = host[1:].partition("]")
        port = rest.lstrip(":")
    else:
        name, _, port = host.partition(":")
    payload = soap_envelope(ctx, body)
    head = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/soap+xml; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n").encode()
    data = b""
    with socket.create_connection((name, int(port or 80)), timeout=timeout) as sock:
        sock.sendall(head + payload)
        while not data.rstrip().endswith(b"Envelope>"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = data.decode(errors="replace")
    status = reply.split("\r\n", 1)[0]
    if " 200 " not in status or "Fault>" in reply:
        raise RuntimeError(f"ONVIF error: {status} {ctx.scrub(reply[-300:])}")
    return reply


def reach(ctx, url):
    """The camera advertises its LAN address: swap in the host and port we actually reach it on."""
    return re.sub(r"//[^/]+/", f"//{ctx.onvif_netloc}/", url, count=1)


def subscribe(ctx):
    """Open a PullPoint subscription and return the URL to pull messages from."""
    device = f"http://{ctx.onvif_netloc}/onvif/device_service"
    caps = soap(ctx, device, '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl">'
                             '<Category>All</Category></GetCapabilities>')
    found = re.search(r"<[^>]*Events[^>]*>\s*<[^>]*XAddr[^>]*>([^<]+)<", caps)
    events_url = reach(ctx, found[1]) if found else f"http://{ctx.onvif_netloc}/onvif/service"
    reply = soap(ctx, events_url,
                 '<CreatePullPointSubscription xmlns="http://www.onvif.org/ver10/events/wsdl">'
                 f'<InitialTerminationTime>PT{SUBSCRIPTION_SECONDS + 30}S</InitialTerminationTime>'
                 '</CreatePullPointSubscription>')
    address = re.search(r"SubscriptionReference>.*?Address[^>]*>([^<]+)<", reply, re.S)
    if not address:
        raise RuntimeError("no subscription address in the ONVIF reply")
    return reach(ctx, address[1])


def motion_states(ctx, reply):
    """Yield (item name, on/off) for every motion-like notification in a PullMessages reply."""
    for note in re.findall(r"<[^>]*NotificationMessage[^>]*>.*?</[^>]*NotificationMessage>", reply, re.S):
        for attributes in re.findall(r"SimpleItem([^>]*)>", note):
            # this camera writes Value before Name, other firmwares do the opposite
            name, value = re.search(r'Name="([^"]*)"', attributes), re.search(r'Value="([^"]*)"', attributes)
            if name and value and name[1].lower() in ctx.motion_events:
                yield name[1], value[1].lower() == "true"


def onvif_loop(ctx, events, stop, report=_ignore):
    last = {}  # the camera repeats the same state in every reply: only pass on changes
    backoff = 5
    while not stop.is_set():
        try:
            pull_url = subscribe(ctx)
            backoff = 5
            report("online", "")
            renew_at = time.monotonic() + SUBSCRIPTION_SECONDS
            while not stop.is_set() and time.monotonic() < renew_at:
                reply = soap(ctx, pull_url, '<PullMessages xmlns="http://www.onvif.org/ver10/events/wsdl">'
                                            '<Timeout>PT1S</Timeout><MessageLimit>20</MessageLimit></PullMessages>',
                             timeout=15)
                for name, value in motion_states(ctx, reply):
                    if last.get(name) != value:
                        last[name] = value
                        events.put((time.time(), name, value))
        except Exception as err:
            message = ctx.scrub(str(err))
            log.warning("onvif: %s (retry in %ss)", message, backoff)
            report("offline", message)
            stop.wait(backoff)
            backoff = min(backoff * 2, 120)


# ---------- ring buffer and events ----------

@dataclass
class Event:
    start: float  # epoch seconds of the first motion
    end: float    # recording stops here, unless late motion pushes it further


def segments(ctx):
    """Ring buffer files as (epoch second the segment starts at, path), oldest first."""
    found = []
    for path in ctx.buffer_dir.glob("*.mp4"):
        match = FILE_RE.match(path.name)
        if match:
            start = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
            found.append((start, path))
    return sorted(found)


def prune_buffer(ctx, event, now):
    """Drop segments that no event can need: keep ctx.pre seconds before now, or before the open event."""
    keep_from = (event.start if event else now) - ctx.pre - 2 * ctx.segment
    for start, path in segments(ctx):
        if start < keep_from:
            path.unlink(missing_ok=True)


def join(parts, out):
    """Concatenate mp4 files without re-encoding. Written as .part first so nobody reads it half done."""
    list_file = out.with_suffix(".txt")
    part = out.with_suffix(".part")
    list_file.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts))
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(list_file), "-c", "copy", "-f", "mp4", str(part)],
                       check=True, capture_output=True)
        part.replace(out)
    finally:
        list_file.unlink(missing_ok=True)
        part.unlink(missing_ok=True)


def save_clip(ctx, event):
    first, last = event.start - ctx.pre, event.end
    parts = [path for start, path in segments(ctx) if start + ctx.segment > first and start < last]
    if not parts:
        log.warning("no buffered video for the event at %s", datetime.fromtimestamp(event.start))
        return
    when = datetime.fromtimestamp(event.start)
    day_dir = ctx.events_dir / f"{when:%Y%m%d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    join(parts, day_dir / f"{when:%H%M%S}.mp4")
    log.info("clip saved: event at %s, %d segments, ends %s", when.strftime("%H:%M:%S"), len(parts),
             datetime.fromtimestamp(event.end).strftime("%H:%M:%S"))


def on_motion(ctx, event, at):
    """Motion starts an event; late motion (inside ctx.extend_within of the end) extends it."""
    if event is None:
        log.info("motion: recording from %ds before to %ds after", ctx.pre, ctx.post)
        return Event(start=at, end=at + ctx.post)
    if event.end - at <= ctx.extend_within:
        log.info("motion near the end of the window: recording %ds more", ctx.post)
        event.end = at + ctx.post
    return event


def controller(ctx, events, stop, report=_ignore):
    event = None
    states = {}  # item name -> on/off, so one detector switching off does not hide another
    last_prune = 0.0
    while not stop.is_set():
        try:
            at, name, value = events.get(timeout=1)
            states[name] = value
            fresh = value  # a new "on"
            if value:
                report("motion", name)
        except queue.Empty:
            at, fresh = time.time(), False
        now = time.time()
        moving = any(states.values())
        if moving and (fresh or event is None or event.end - at <= ctx.extend_within):
            # a new "motion on", or motion that is still going when the window is almost over
            event = on_motion(ctx, event, at)
        # wait for the segment that holds the end of the event to be closed
        if event and now >= event.end + ctx.segment + 3:
            try:
                save_clip(ctx, event)
            except Exception as err:
                log.exception("could not save the clip")
                report("error", f"could not save the clip: {ctx.scrub(str(err))}")
            event = None
        if now - last_prune > 10:
            prune_buffer(ctx, event, now)
            last_prune = now


# ---------- one video per day ----------

def clip_seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True)
    return float(out.stdout.strip() or 0)


def timestamp(seconds):
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def due_days(ctx, force=False):
    now = datetime.now()
    for day_dir in sorted(ctx.events_dir.glob("[0-9]" * 8)):
        day = datetime.strptime(day_dir.name, "%Y%m%d").date()
        if force or day < now.date() or now.time() >= ctx.upload_at:
            yield day_dir


def storage_for(ctx, state=None):
    """The destination of this camera's daily video: the one the context carries, YouTube otherwise."""
    if ctx.storage is not None:
        return ctx.storage
    from tapo_storage.youtube import YouTubeStorage
    return YouTubeStorage(ctx, state)


def upload_day(ctx, storage, day_dir, report=_ignore):
    clips = sorted(day_dir.glob("[0-9]*.mp4"))
    if not clips:
        day_dir.rmdir()
        return
    first = datetime.strptime(day_dir.name + clips[0].stem, "%Y%m%d%H%M%S")
    lines, offset = [], 0.0
    for clip in clips:
        began = datetime.strptime(day_dir.name + clip.stem, "%Y%m%d%H%M%S")
        lines.append(f"{timestamp(offset)} Movimento alle {began:%H:%M:%S}")
        offset += clip_seconds(clip)
    last_end = datetime.strptime(day_dir.name + clips[-1].stem, "%Y%m%d%H%M%S").timestamp() \
        + clip_seconds(clips[-1])
    title = (f"{ctx.playlist_prefix} {first:%Y-%m-%d} eventi "
             f"{first:%H:%M}-{datetime.fromtimestamp(last_end):%H:%M} ({len(clips)})")
    description = "\n".join(lines[:120])  # YouTube allows 5000 characters
    day_file = day_dir / "day.mp4"
    join(clips, day_file)
    storage.put(day_file, f"{first:%Y-%m-%d}", title, description)
    report("uploaded", title)
    # delete only what was uploaded: a clip saved meanwhile waits for the next upload
    for path in clips + [day_file]:
        path.unlink(missing_ok=True)
    try:
        day_dir.rmdir()
    except OSError:
        pass


def upload_pending(ctx, state, force=False, report=_ignore, gate=None):
    """Upload the days that are due. A `gate` (multi-account mode) keeps the shared YouTube quota: it says when
    this camera may upload and is charged after every upload."""
    days = list(due_days(ctx, force))
    storage = storage_for(ctx, state)
    if not storage.uses_quota:
        gate = None  # only YouTube spends the shared API quota
    if not days or (gate and not gate.allowed()):
        return
    storage.connect()
    for day_dir in days:
        if gate and not gate.allowed():
            return
        upload_day(ctx, storage, day_dir, report)
        if gate:
            gate.charge()


STORAGE_RETRY_SECONDS = 600   # a destination that refuses the upload (full, unreachable): try again in ten minutes


def daily_loop(ctx, stop, report=_ignore, gate=None):
    state = base.load_state(ctx)
    while not stop.is_set():
        delay = 60
        try:
            upload_pending(ctx, state, report=report, gate=gate)
        except HttpError as err:
            if base.is_quota_error(err):
                log.error("YouTube quota exceeded, sleeping 1h")
                report("error", "YouTube quota exceeded")
                if gate:
                    gate.exhausted()  # Google says it is gone: the other cameras stop trying today too
                stop.wait(3600)
                continue
            log.exception("YouTube error")
            report("error", f"YouTube error: {ctx.scrub(str(err))}")
        except StorageError as err:
            log.error("upload failed: %s", ctx.scrub(str(err)))
            report("error", ctx.scrub(str(err)))
            delay = STORAGE_RETRY_SECONDS   # the clips stay here and are offered again, they are not deleted
        except Exception as err:
            log.exception("daily upload failed")
            report("error", ctx.scrub(str(err)))
        stop.wait(delay)


# ---------- commands ----------

def cmd_listen():
    ctx = CameraContext.from_env()
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    events, stop = queue.Queue(), threading.Event()
    threading.Thread(target=onvif_loop, args=(ctx, events, stop), daemon=True).start()
    print(f"listening {seconds}s for motion events from {ctx.onvif_address}:{ctx.onvif_port} ...")
    end = time.time() + seconds
    while time.time() < end:
        try:
            at, name, moving = events.get(timeout=1)
            print(f"{datetime.fromtimestamp(at):%H:%M:%S} {name} {'ON' if moving else 'off'}")
        except queue.Empty:
            pass
    stop.set()


def cmd_upload():
    ctx = CameraContext.from_env()
    ctx.ensure_dirs()
    upload_pending(ctx, base.load_state(ctx), force=True)


def health_server(port):
    """Answer GET anything with 200 OK. Free web services of Render sleep after 15 minutes without
    inbound HTTP traffic: an external pinger (UptimeRobot, cron-job.org) hitting this keeps it awake."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # a ping every few minutes would flood the log
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("health endpoint on port %d", port)


def cmd_run():
    from camera_worker import CameraWorker
    ctx = CameraContext.from_env()
    try:
        storage_for(ctx).check()  # fail fast if the destination is not usable (YouTube: not authorised)
    except StorageError as err:
        sys.exit(str(err))
    if os.getenv("PORT"):  # set by web-service hosts (Render): they need something listening
        health_server(int(os.environ["PORT"]))
    ctx.ensure_dirs()
    for _, path in segments(ctx):
        path.unlink(missing_ok=True)  # leftovers of a previous run
    worker = CameraWorker(ctx, lambda kind, detail: None)
    worker.start()
    try:
        while worker.alive:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping")
        worker.stop()


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    commands = {"listen": cmd_listen, "run": cmd_run, "upload": cmd_upload}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
