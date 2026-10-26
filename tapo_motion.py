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
from tapo_uploader import DATA, FILE_RE, env, log, scrub

PRE = int(env("MOTION_PRE_SECONDS", "30"))
POST = int(env("MOTION_POST_SECONDS", "300"))
EXTEND_WITHIN = int(env("MOTION_EXTEND_WITHIN", "30"))
SEGMENT = int(env("BUFFER_SEGMENT_SECONDS", "10"))
UPLOAD_AT = datetime.strptime(env("UPLOAD_AT", "21:00"), "%H:%M").time()
# ONVIF items that count as "something moves": IsMotion = motion detection, IsPeople = person
# detection. Which ones the camera sends depends on what is switched on in the Tapo app.
MOTION_EVENTS = {name.strip().lower() for name in env("MOTION_EVENTS", "IsMotion,IsPeople").split(",")}
BUFFER_DIR = DATA / env("BUFFER_DIR", "buffer")
EVENTS_DIR = DATA / env("EVENTS_DIR", "events")
# ONVIF can sit behind a different host/port than RTSP (e.g. a second port forward)
ONVIF_HOST = env("ONVIF_HOST", env("CAMERA_HOST"))
ONVIF_PORT = env("ONVIF_PORT", "2020")
# the camera's subscription is short-lived: ask for a new one before it expires
SUBSCRIPTION_SECONDS = 90

WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
BASE64_ENCODING = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-soap-message-security-1.0#Base64Binary"


# ---------- ONVIF motion events ----------

def soap_envelope(body):
    """SOAP message with a WS-Security UsernameToken (password digest), as ONVIF requires."""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    password = env("CAMERA_PASS")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Header>'
        f'<wsse:Security xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}"><wsse:UsernameToken>'
        f'<wsse:Username>{env("CAMERA_USER")}</wsse:Username>'
        f'<wsse:Password Type="{PASSWORD_DIGEST}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{BASE64_ENCODING}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
        f'<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>'
        f'<s:Body>{body}</s:Body></s:Envelope>'
    ).encode()


def soap(url, body, timeout=30):
    """POST a SOAP message over a plain socket.

    http.client and urllib hang on this camera: it does not close the connection or give a usable
    Content-Length for every reply, so we read until the end of the SOAP envelope instead.
    """
    host, path = re.match(r"https?://([^/]+)(/.*)", url).groups()
    name, _, port = host.partition(":")
    payload = soap_envelope(body)
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
        raise RuntimeError(f"ONVIF error: {status} {scrub(reply[-300:])}")
    return reply


def reach(url):
    """The camera advertises its LAN address: swap in the host and port we actually reach it on."""
    return re.sub(r"//[^/]+/", f"//{ONVIF_HOST}:{ONVIF_PORT}/", url, count=1)


def subscribe():
    """Open a PullPoint subscription and return the URL to pull messages from."""
    device = f"http://{ONVIF_HOST}:{ONVIF_PORT}/onvif/device_service"
    caps = soap(device, '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl">'
                        '<Category>All</Category></GetCapabilities>')
    found = re.search(r"<[^>]*Events[^>]*>\s*<[^>]*XAddr[^>]*>([^<]+)<", caps)
    events_url = reach(found[1]) if found else f"http://{ONVIF_HOST}:{ONVIF_PORT}/onvif/service"
    reply = soap(events_url,
                 '<CreatePullPointSubscription xmlns="http://www.onvif.org/ver10/events/wsdl">'
                 f'<InitialTerminationTime>PT{SUBSCRIPTION_SECONDS + 30}S</InitialTerminationTime>'
                 '</CreatePullPointSubscription>')
    address = re.search(r"SubscriptionReference>.*?Address[^>]*>([^<]+)<", reply, re.S)
    if not address:
        raise RuntimeError("no subscription address in the ONVIF reply")
    return reach(address[1])


def motion_states(reply):
    """Yield (item name, on/off) for every motion-like notification in a PullMessages reply."""
    for note in re.findall(r"<[^>]*NotificationMessage[^>]*>.*?</[^>]*NotificationMessage>", reply, re.S):
        for attributes in re.findall(r"SimpleItem([^>]*)>", note):
            # this camera writes Value before Name, other firmwares do the opposite
            name, value = re.search(r'Name="([^"]*)"', attributes), re.search(r'Value="([^"]*)"', attributes)
            if name and value and name[1].lower() in MOTION_EVENTS:
                yield name[1], value[1].lower() == "true"


def onvif_loop(events, stop):
    last = {}  # the camera repeats the same state in every reply: only pass on changes
    backoff = 5
    while not stop.is_set():
        try:
            pull_url = subscribe()
            backoff = 5
            renew_at = time.monotonic() + SUBSCRIPTION_SECONDS
            while not stop.is_set() and time.monotonic() < renew_at:
                reply = soap(pull_url, '<PullMessages xmlns="http://www.onvif.org/ver10/events/wsdl">'
                                       '<Timeout>PT1S</Timeout><MessageLimit>20</MessageLimit></PullMessages>',
                             timeout=15)
                for name, value in motion_states(reply):
                    if last.get(name) != value:
                        last[name] = value
                        events.put((time.time(), name, value))
        except Exception as err:
            log.warning("onvif: %s (retry in %ss)", scrub(str(err)), backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 120)


# ---------- ring buffer and events ----------

@dataclass
class Event:
    start: float  # epoch seconds of the first motion
    end: float    # recording stops here, unless late motion pushes it further


def segments():
    """Ring buffer files as (epoch second the segment starts at, path), oldest first."""
    found = []
    for path in BUFFER_DIR.glob("*.mp4"):
        match = FILE_RE.match(path.name)
        if match:
            start = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
            found.append((start, path))
    return sorted(found)


def prune_buffer(event, now):
    """Drop segments that no event can need: keep PRE seconds before now, or before the open event."""
    keep_from = (event.start if event else now) - PRE - 2 * SEGMENT
    for start, path in segments():
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


def save_clip(event):
    first, last = event.start - PRE, event.end
    parts = [path for start, path in segments() if start + SEGMENT > first and start < last]
    if not parts:
        log.warning("no buffered video for the event at %s", datetime.fromtimestamp(event.start))
        return
    when = datetime.fromtimestamp(event.start)
    day_dir = EVENTS_DIR / f"{when:%Y%m%d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    join(parts, day_dir / f"{when:%H%M%S}.mp4")
    log.info("clip saved: event at %s, %d segments, ends %s", when.strftime("%H:%M:%S"), len(parts),
             datetime.fromtimestamp(event.end).strftime("%H:%M:%S"))


def on_motion(event, at):
    """Motion starts an event; late motion (inside EXTEND_WITHIN of the end) extends it."""
    if event is None:
        log.info("motion: recording from %ds before to %ds after", PRE, POST)
        return Event(start=at, end=at + POST)
    if event.end - at <= EXTEND_WITHIN:
        log.info("motion near the end of the window: recording %ds more", POST)
        event.end = at + POST
    return event


def controller(events, stop):
    event = None
    states = {}  # item name -> on/off, so one detector switching off does not hide another
    last_prune = 0.0
    while not stop.is_set():
        try:
            at, name, value = events.get(timeout=1)
            states[name] = value
            fresh = value  # a new "on"
        except queue.Empty:
            at, fresh = time.time(), False
        now = time.time()
        moving = any(states.values())
        if moving and (fresh or event is None or event.end - at <= EXTEND_WITHIN):
            # a new "motion on", or motion that is still going when the window is almost over
            event = on_motion(event, at)
        # wait for the segment that holds the end of the event to be closed
        if event and now >= event.end + SEGMENT + 3:
            try:
                save_clip(event)
            except Exception:
                log.exception("could not save the clip")
            event = None
        if now - last_prune > 10:
            prune_buffer(event, now)
            last_prune = now


# ---------- one video per day ----------

def clip_seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True)
    return float(out.stdout.strip() or 0)


def timestamp(seconds):
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def due_days(force=False):
    now = datetime.now()
    for day_dir in sorted(EVENTS_DIR.glob("[0-9]" * 8)):
        day = datetime.strptime(day_dir.name, "%Y%m%d").date()
        if force or day < now.date() or now.time() >= UPLOAD_AT:
            yield day_dir


def upload_day(yt, state, day_dir):
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
    title = (f"{env('PLAYLIST_PREFIX', 'Tapo')} {first:%Y-%m-%d} eventi "
             f"{first:%H:%M}-{datetime.fromtimestamp(last_end):%H:%M} ({len(clips)})")
    description = "\n".join(lines[:120])  # YouTube allows 5000 characters
    day_file = day_dir / "day.mp4"
    join(clips, day_file)
    base.upload(yt, state, day_file, first, title=title, description=description)
    # delete only what was uploaded: a clip saved meanwhile waits for the next upload
    for path in clips + [day_file]:
        path.unlink(missing_ok=True)
    try:
        day_dir.rmdir()
    except OSError:
        pass


def upload_pending(state, force=False):
    days = list(due_days(force))
    if not days:
        return
    yt = base.youtube_client()
    for day_dir in days:
        upload_day(yt, state, day_dir)


def daily_loop(stop):
    state = base.load_state()
    while not stop.is_set():
        try:
            upload_pending(state)
        except HttpError as err:
            if base.is_quota_error(err):
                log.error("YouTube quota exceeded, sleeping 1h")
                stop.wait(3600)
                continue
            log.exception("YouTube error")
        except Exception:
            log.exception("daily upload failed")
        stop.wait(60)


# ---------- commands ----------

def cmd_listen():
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    events, stop = queue.Queue(), threading.Event()
    threading.Thread(target=onvif_loop, args=(events, stop), daemon=True).start()
    print(f"listening {seconds}s for motion events from {ONVIF_HOST}:{ONVIF_PORT} ...")
    end = time.time() + seconds
    while time.time() < end:
        try:
            at, name, moving = events.get(timeout=1)
            print(f"{datetime.fromtimestamp(at):%H:%M:%S} {name} {'ON' if moving else 'off'}")
        except queue.Empty:
            pass
    stop.set()


def cmd_upload():
    upload_pending(base.load_state(), force=True)


def cmd_run():
    base.youtube_client()  # fail fast if not authorised
    BUFFER_DIR.mkdir(exist_ok=True)
    EVENTS_DIR.mkdir(exist_ok=True)
    for _, path in segments():
        path.unlink(missing_ok=True)  # leftovers of a previous run
    events, stop = queue.Queue(), threading.Event()
    threads = [
        threading.Thread(target=base.record_loop, args=(BUFFER_DIR, SEGMENT, stop)),
        threading.Thread(target=onvif_loop, args=(events, stop)),
        threading.Thread(target=controller, args=(events, stop)),
        threading.Thread(target=daily_loop, args=(stop,)),
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
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    commands = {"listen": cmd_listen, "run": cmd_run, "upload": cmd_upload}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
