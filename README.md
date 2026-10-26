# tapo-uploader: Tapo camera to YouTube uploader

🌐 **English** · [Italiano](README_IT.md)

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![ffmpeg](https://img.shields.io/badge/ffmpeg-no%20re--encoding-orange.svg)](https://ffmpeg.org/)
[![YouTube Data API v3](https://img.shields.io/badge/YouTube%20Data%20API-v3-red.svg)](https://developers.google.com/youtube/v3)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](Dockerfile)

**Record your TP-Link Tapo camera (tested on the Tapo C200) only when it detects motion, and upload
the footage to YouTube as private videos, with one playlist per day. No cloud subscription, no
firmware hacking, no storage to buy.**

The Tapo C200 can't upload to YouTube or Google Drive by itself. `tapo-uploader` is a small Python
service that pulls the camera's **RTSP** stream with **ffmpeg**, listens for **ONVIF** motion
events, and publishes the result through the official **YouTube Data API v3**. After each upload the
local file is deleted, so it runs for free on a Raspberry Pi, a cheap VPS, a mini PC or a Docker
container.

```
 Tapo C200 ──RTSP──▶ ffmpeg ──▶ 30 s before + 5 min after each motion ──▶ one private YouTube video a day
 (home)    ──ONVIF motion events──▶                                       playlist "Tapo 2026-10-26"
                                                                          (local files deleted after upload)
```

## Features

- **Motion-triggered recording.** Keeps 30 seconds *before* the detection and 5 minutes *after*;
  motion near the end of the window extends it. All times are configurable.
- **One video per day.** Clips are joined without re-encoding and uploaded once a day (21:00 by
  default), with the time of every event as YouTube chapters in the description.
- **Or continuous recording.** 6-hour segments, each uploaded as its own video.
- **Private by default.** Videos go to your own channel as `private`, grouped in a daily playlist
  named `Tapo YYYY-MM-DD`.
- **Frees the disk.** Files are deleted right after a successful upload.
- **No re-encoding.** `ffmpeg -c copy`: close to zero CPU, so a Raspberry Pi is plenty.
- **Resilient.** Restarts ffmpeg when the stream drops, retries failed uploads, handles the YouTube
  quota, survives reboots (fragmented MP4 stays playable even if killed).
- **Credentials never logged.** The camera password is masked in every log line.
- **Runs anywhere.** systemd unit, `Dockerfile`, and Render blueprints included.
- **A complete, honest guide.** Every step of a real setup is documented, including the dead ends.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [1. The camera](#1-the-camera)
- [2. Google Cloud and YouTube](#2-google-cloud-and-youtube)
- [3. Install and test on your PC](#3-install-and-test-on-your-pc)
- [Where to run it](#where-to-run-it)
- [4. The server](#4-the-server)
- [5. Reaching the camera from the internet](#5-reaching-the-camera-from-the-internet)
- [6. Final check](#6-final-check)
- [Motion mode: one video per day](#motion-mode-one-video-per-day)
- [Multi-account mode](#multi-account-mode)
- [Where the recordings go](#where-the-recordings-go)
- [Daily use](#daily-use)
- [Configuration](#configuration)
- [Under the hood](#under-the-hood)
- [YouTube quota](#youtube-quota)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [License](#license)

---

## How it works

There are **two modes**. Both use the same camera, the same YouTube login and the same `.env`.

| | Continuous (`tapo_uploader.py`) | Motion (`tapo_motion.py`) |
|---|---|---|
| Records | Everything, in 6-hour segments | Only around motion: 30 s before, 5 min after |
| Uploads | Each segment as soon as it closes | One video a day, at a fixed time |
| Disk use | A few GB at most | Almost nothing (about one minute of buffer) |
| Needs | RTSP | RTSP + ONVIF events (port 2020) |
| YouTube quota | 4 uploads/day | 1 upload/day |

`tapo_uploader.py run` starts two threads:

1. **Recording.** ffmpeg reads the RTSP stream with `-c copy` (the video is not re-encoded) and
   writes `YYYYMMDD_HHMMSS.mp4` files into `recordings/`. Cuts are aligned to the wall clock: with
   6-hour segments they close at 00:00, 06:00, 12:00 and 18:00. If the stream drops, ffmpeg restarts.
2. **Upload.** Every minute it looks for finished segments, uploads them as `private` videos titled
   `Tapo YYYY-MM-DD HH:MM-HH:MM`, adds them to the playlist `Tapo YYYY-MM-DD` (created if missing)
   and **deletes the file**. If an upload fails the file stays and is retried.

The camera can't upload on its own, so you need an always-on computer that can reach it: a Raspberry
Pi or mini PC at home, a VPS, or a container host.

## Requirements

| You need | Notes |
|---|---|
| A Tapo camera with RTSP | Tested on the **Tapo C200**. You need the "Camera Account" (step 1). |
| An always-on computer | Linux recommended (VPS, Raspberry Pi). Python 3.10+ and ffmpeg. |
| A Google account with a YouTube channel | The videos land in its channel. |
| Router access (only if the server is not at home) | To open a port towards the camera. |

Disk: at 640×360 a 6-hour segment is about 0.4 GB. A few free GB are enough because files are
deleted after upload. Bandwidth: about 1 MB per minute of video.

Other ONVIF cameras that expose RTSP may work, but only the Tapo C200 has been tested.

---

## 1. The camera

1. In the Tapo app open the camera, then **Settings (gear) → Advanced → Camera Account**. Create a
   username and password **just for the camera**. They are *not* your Tapo/cloud account: using
   those is the most common mistake and gives `401 Unauthorized`.
2. Find the camera's IP address in the app (Device Info) or in your router, and ideally give it a
   fixed IP (DHCP reservation) in the router.
3. Set the video quality to **High** (Settings → Video Quality). It may default to low: in our case
   both streams were 640×360.
4. The stream address is:

   ```
   rtsp://USER:PASSWORD@CAMERA_IP:554/stream1     (high quality)
   rtsp://USER:PASSWORD@CAMERA_IP:554/stream2     (low quality)
   ```

   If the password contains special characters (`!`, `@`, `:`) they must be URL-encoded. The script
   does it for you; by hand, a letters-and-digits password is easier.

> **Important limit:** the camera accepts **one RTSP session per stream**. While the service records
> `stream1`, a second program opening `stream1` gets `Operation not permitted`. To experiment while
> the service runs, use `stream2` (`CAMERA_STREAM=stream2`) or stop the service first.

---

## 2. Google Cloud and YouTube

The script uploads to your channel through the official YouTube Data API v3, so you need a small
Google "project" that identifies the app. It is free.

### 2.1 Project and API

1. Go to <https://console.cloud.google.com/projectcreate>, name it `tapo-uploader`, **Create**.
2. Open <https://console.cloud.google.com/apis/library/youtube.googleapis.com> and press **Enable**
   (with the new project selected at the top).

### 2.2 OAuth consent screen

1. Open <https://console.cloud.google.com/auth/overview> and press **Get started**.
2. App name `tapo-uploader`, support email = yours.
3. Audience: **External**. Contact email = yours. Accept and **Create**.
4. Under **Audience → Test users → Add users** add **your own Google account**, the one that owns
   the YouTube channel.

### 2.3 Credentials

1. **Clients → Create client**. Type: **Desktop app**, name `tapo-uploader`. **Create**.
2. Press **Download JSON** and save it as `client_secret.json` in the project folder. It is a
   secret: don't commit it (`.gitignore` excludes it).

### 2.4 Publish the app (important)

While the app stays in **Testing**, the access token **expires after 7 days** and the script stops
uploading until you log in again. To avoid it, publish the app ("In production"). For personal use
Google needs no verification, but the **Publish app** button stays disabled until **Branding** has:

- a link to the app **home page**,
- a link to the **privacy policy**,
- the matching **authorized domain**.

> **Use your own pages, not this project's.** Google checks that the home page and the privacy policy belong to
> the domain of whoever owns the app, and that they describe *that* app. The policy in this repository names its
> author as the person responsible and describes the author's own server, so it would be wrong on your app and
> could fail Google's review. Copy `index.html` and `privacy.html` (MIT licensed) and **rewrite the privacy policy
> for your own use**: who runs it, what is stored, and who can use it (just you, or other people on your server:
> then say so, and name the services involved). A link back to this project on your home page is appreciated but
> not required. Each person who runs the software needs their own Google app and their own pages.

You don't need a real website, only two static pages. The quickest way is **GitHub Pages**:

1. Create a public repository (this one works) with two files: `index.html` (one line about what
   the app does) and `privacy.html` (what it accesses, what is stored, how to revoke access). This
   repository has both, ready to copy and adapt.
2. In *Settings → Pages* choose the `main` branch, folder `/`.
3. After a minute the pages are at `https://YOURUSER.github.io/REPO-NAME/`.
4. In Google Cloud, **Branding → App domain**: enter the home page and privacy policy; under
   **Authorized domains → Add domain** put `YOURUSER.github.io`. Save.
5. **Audience → Publish app → Confirm** ("Push to production?").

> Publish **before** creating the token (step 3). A token created while the app was in Testing can
> stay subject to the 7-day expiry even after publishing: to be safe, delete `token.json` and run
> `auth` again after publishing.

---

## 3. Install and test on your PC

These steps work on any system (Windows, macOS, Linux). You need Python 3.10+ and **ffmpeg** in the
PATH (`ffmpeg -version` to check).

```bash
git clone https://github.com/LuigiElleBalotta/tapo-uploader.git
cd tapo-uploader
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill in CAMERA_USER, CAMERA_PASS, CAMERA_HOST
```

Put `client_secret.json` (step 2.3) in the folder, then:

```bash
python tapo_uploader.py check   # 1. does the camera answer? prints codec and resolution
python tapo_uploader.py auth    # 2. Google login, once: creates token.json
python tapo_uploader.py test    # 3. records 60 s and uploads them: checks everything
```

- **`check`** must print `codec_name=h264`, the resolution and the audio. `401 Unauthorized` means
  wrong credentials; `Operation not permitted` means the stream is already in use (see above).
- **`auth`** prints an address: open it in a browser **on the same computer**, choose the Google
  account of the channel and allow YouTube access. The browser shows "The authentication flow has
  completed" and `token.json` appears. If, while in Testing, you see "Google hasn't verified this
  app": *Advanced → Go to tapo-uploader*.
- **`test`** records 60 seconds, uploads them as a private video and creates the day's playlist.
  Check that the video is on YouTube, then delete it from Studio.

If the server has no browser, run `auth` on your PC and copy `token.json` to the server.

---

## Where to run it

The program must stay on 24/7 and be able to reach the camera. The options, simplest first:

| Where | Rough cost | How it reaches the camera | Notes |
|---|---|---|---|
| **Raspberry Pi / mini PC at home** | one-off, about $15–50 | LAN | **Recommended**: no port forwarding, no DDNS, no exposed camera. |
| **VPS** (as in this guide) | a few dollars a month | port forwarding limited to the VPS's fixed IP | Steps 4 and 5. This is the tested setup. |
| **Render** (Background Worker) | about $7/month, plus $0.25/GB of disk | port forwarding to Render's shared IPs | Docker image included, **not tested**. See below. |
| **Render free** (web service) | $0 | port forwarding to Render's shared IPs | Tested up to startup, not with the camera. Ephemeral disk: clips can be lost. See below. |
| **Vercel** | – | – | **Not suitable.** See below. |

Other container platforms (Fly.io, Railway) follow the same pattern as Render: the `Dockerfile`
works anywhere, only prices and networking change.

### Render (paid Background Worker)

Render can run an always-on process (a *Background Worker*) from a Docker image. The repository
contains `Dockerfile` and `render.yaml`. Things to know:

- **Cost.** The smallest worker (`0.5c-512mb`: 0.5 CPU and 512 MB) is listed at $7/month. Render's
  free plan is for web services only, not workers. With ffmpeg in `-c copy` mode CPU is not the
  limit; RAM was not measured on Render.
- **Disk.** The container's disk is lost on every restart and deploy. Without a **persistent disk**
  ($0.25/GB per month, 1 GB is enough; it attaches to a worker) the token, the state and the day's
  clips are lost. `render.yaml` asks for one mounted at `/data`, and the script uses it through
  `DATA_DIR`.
- **Secrets.** A container has no files to copy: `token.json` and `client_secret.json` are passed as
  the environment variables `YOUTUBE_TOKEN_JSON` and `YOUTUBE_CLIENT_SECRET_JSON` (the files'
  contents). On first start the script writes them to `/data` with mode `600`; afterwards the
  refreshed token stays on disk. The login (`auth`) is done once on your PC.
- **Networking is the weak point.** Render's outbound IPs are **shared by every service in the same
  region**, and a service may use any address in its range (see
  [Render's docs](https://render.com/docs/outbound-ip-addresses)). To restrict access to the camera
  you would open port forwarding to **all of the region's ranges**, so to other Render customers
  too. Dedicated IPs exist but, from the information found, are paid and only on Pro/Scale plans
  (about $100/month per set of IPs): not worth it for this. If you pick Render, the exposed port
  must be protected by a **long, unique password** and a non-standard external port; or connect home
  and Render with a VPN, which was not verified.

Steps: connect the repository to Render (**New → Blueprint**), fill in the variables it asks for
(`CAMERA_USER`, `CAMERA_PASS`, `CAMERA_HOST`/`CAMERA_PORT`, `ONVIF_HOST`/`ONVIF_PORT`,
`YOUTUBE_TOKEN_JSON`, `YOUTUBE_CLIENT_SECRET_JSON`), open the router ports to Render's IPs and watch
the worker's logs. Render only asks for the variables when the Blueprint is created: later you
change them in the dashboard. For continuous recording instead of motion mode, uncomment
`dockerCommand` in `render.yaml`.

### Render free (no VPS)

For people without a server. The **VPS remains the main choice**: this is a zero-cost alternative
with precise limits. Deploying as a *web service* on the `free` plan was tried on Render up to
startup (build, `health endpoint on port 10000`, `ffmpeg start`); **recording from the camera
through Render was not verified**, because it needs the router rules described below.

How it works: the program opens a small HTTP port (`/health`, active when Render sets `PORT`) and
a free monitoring service (UptimeRobot, cron-job.org) calls it every 5 minutes. Per
[Render's documentation](https://render.com/docs/free) a free service sleeps after 15 minutes
**without inbound traffic**; outbound traffic doesn't count, so a call from outside is needed. The
750 free monthly hours are enough for one always-on service.

**Steps (the ones actually done, from the dashboard):**

1. On [dashboard.render.com](https://dashboard.render.com) choose **New → Web Service**, the
   **Public Git Repository** tab, paste the repository URL and press **Connect**. Render detects the
   `Dockerfile` by itself.
2. Under **Compute** pick the **$0 / month (0.1 CPU, 512 MB)** row. The preselected plan is the
   $7/month one: if you leave it, you pay.
3. Under **Environment Variables** press **Add from .env** and paste the lines below with your
   values. The token and client secret must be on **a single line**, as compact JSON
   (`python -c "import json;print(json.dumps(json.load(open('token.json')),separators=(',',':')))"`).
   ```
   CAMERA_USER=...
   CAMERA_PASS=...
   CAMERA_HOST=your-ddns-name.example.com
   CAMERA_PORT=external RTSP port
   ONVIF_HOST=your-ddns-name.example.com
   ONVIF_PORT=external ONVIF port
   CAMERA_STREAM=stream1
   DATA_DIR=/data
   TZ=Europe/Rome
   YOUTUBE_TOKEN_JSON=...
   YOUTUBE_CLIENT_SECRET_JSON=...
   ```
4. **Deploy web service**. The build takes about a minute and a half. In the logs you should see
   `health endpoint on port 10000` and `ffmpeg start`. The warning `file_cache is only supported
   with oauth2client<4.0.0` is harmless.
5. From the service page take the URL (`https://NAME.onrender.com`) and, with the **Connect →
   Outbound** button, the IP ranges to give to the router (in our case `74.220.51.0/24` and
   `74.220.59.0/24`: yours depend on the region and may differ).
6. Create router rules for the camera's two ports, one per range (see below).
7. Create the monitor (UptimeRobot, cron-job.org) on `https://NAME.onrender.com/health` every
   5 minutes.

**If you already run a VPS with this camera:** the camera accepts one RTSP session per stream. If
the VPS and Render both record `stream1`, whichever connects second gets
`Operation not permitted`. Run a single service per stream, or set `CAMERA_STREAM=stream2` on
Render.

The `render.free.yaml` Blueprint was not tried: opening a Blueprint with the default `render.yaml`
(the $7/month worker) makes Render ask for a payment card. The web service created by hand, as
above, did not.

**Limits and risks, read them first:**

| Limit | Consequence |
|---|---|
| The disk is wiped on every restart, deploy or spin-down | Clips of the day not yet uploaded are **lost**. Render may restart the service at any time. |
| If the service sleeps (monitor stopped, hours used up) | No recording until it wakes up (about a minute after the first request). |
| Free hours used up | All free services are suspended until next month. Fine with one service and the monitor, not with two. |
| Render may suspend free services with very high outbound traffic | Here it is a few MB a day, but rules can change. |
| The refreshed YouTube token is not persisted | The token comes from `YOUTUBE_TOKEN_JSON`; every restart starts from it. It works until it is revoked. |
| RAM and CPU of the free plan are not documented | Not measured: ffmpeg in `-c copy` is light, but not verified. |
| The external monitor is one more dependency | If the monitoring service changes its rules, the service sleeps. |
| **Render's IPs are shared and not fixed** | See below: it is the most serious risk. |

**The most serious risk is the camera's security.** Render's outbound IPs are shared by every
service in the region and are not in the documentation: you find them in the service's dashboard,
**Connect → Outbound**. You have two options, both with a price:

1. **Router rules for each range** (recommended). One port-forwarding rule per range and per port,
   with the source limited to that range. Even so you stay exposed to anyone using the same range
   on Render, and ranges can change: check from time to time. It was not verified that your router
   accepts several rules on the same external port with different sources; if it doesn't, use a
   different external port for each rule.
2. **Ports open to everyone.** Only the password protects the camera: use a **long, unique
   password** (never one used elsewhere), a non-standard external port, and watch for login
   attempts. Not recommended.

If the connection to the camera drops, the script retries by itself with growing delays: only that
stretch is lost. If your home IP changes, the DDNS name (step 5.1) follows the update.

To get the same results as the VPS without these limits you need a persistent disk (paid, see
above) or a Raspberry Pi at home.

### Vercel

The answer here is no, and it is about how the platform works, not about configuration.

- **Functions have a maximum duration.** With Fluid compute the limit is 300 s on Hobby and 800 s on
  Pro, with 1800 s in beta ([limits](https://vercel.com/docs/functions/limitations)). A function
  answers a request and is shut down: it can't keep ffmpeg attached to the camera all day.
- **Why "non-serverless" NestJS projects worked.** A NestJS app on Vercel still runs as a function:
  it serves HTTP requests that last a few seconds. A service that records continuously is a
  different thing.
- **The disk is temporary.** There is nowhere to keep the video ring buffer or the clips until the
  evening upload.
- **Vercel Sandbox** is a microVM that can run background processes, but lasts at most 45 minutes
  on Hobby or a few hours on Pro, and files vanish when it stops. You could chain one sandbox after
  another and save clips to Vercel Blob, but you would get gaps at every handover, the cost of an
  always-on process and **non-fixed outbound IPs**, so port forwarding couldn't be restricted. Not
  recommended.
- **Vercel Workflows** pauses and resumes long jobs, but it is not a place to run a recording
  process.

Vercel can still serve a status page or an API that reads the state of the videos, with the
recording running elsewhere.

---

## 4. The server

Example for Ubuntu/Debian (a VPS, a Raspberry Pi, a mini PC). Replace `USER` with your Linux user.

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg python3-venv      # python3-venv is really needed on Ubuntu

mkdir -p ~/tapo-uploader && cd ~/tapo-uploader
# copy here: tapo_uploader.py, tapo_motion.py, camera_context.py, camera_worker.py, requirements.txt, .env, client_secret.json, token.json
chmod 600 .env client_secret.json token.json     # only you can read them

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The server's `.env` is the PC's one, with `CAMERA_HOST` and `CAMERA_PORT` pointing to the camera
**as seen from the internet** (step 5), not to the home IP.

### systemd service

So it starts at boot and restarts by itself if it stops (no cron needed).

```bash
sudo cp deploy/tapo-uploader.service /etc/systemd/system/
sudo nano /etc/systemd/system/tapo-uploader.service   # set USER and the paths
sudo systemctl daemon-reload
sudo systemctl enable tapo-uploader
```

**Don't start it yet**: first step 5 and the check in step 6.

The file sets `Environment=TZ=Europe/Rome`: without it, a server on UTC cuts segments at 1 AM
Italian time and changes playlist at UTC midnight. Change the timezone as needed.

---

## 5. Reaching the camera from the internet

If the server is on the same network as the camera, skip this step and use the local IP. Otherwise
the camera, which sits behind your home router, must be made reachable **only from your server**.

> The Tapo app works away from home because the camera connects *outbound* to TP-Link's servers.
> That doesn't apply to RTSP: you need an open port on the router.

### 5.1 A fixed name for your home IP (DDNS)

Your home's public IP usually changes. A free DDNS service gives a fixed name that follows the IP.
With **FreeDNS** (<https://freedns.afraid.org>, free, no monthly confirmations):

1. Sign up, then **Subdomains → Add**: type `A`, pick a free domain and a hard-to-guess name (for
   example `cam-` plus random characters). Destination: your current IP.
2. Under **Dynamic DNS** copy your record's token: it is the part after the `?` in the "Direct URL"
   (they call it "hash").
3. In the router's **DDNS** section: provider `freedns`, domain name = the full subdomain, hash =
   the token. Apply: the status should say the update succeeded.

Check that the name resolves to your IP: `nslookup your-name.domain.com`.

> If the "WAN" IP shown by the router differs from the public IP you see on a site like
> `ifconfig.me`, you are behind **CGNAT** and no port forwarding can work: use a VPN (Tailscale,
> WireGuard) between home and server, or run the script at home.

### 5.2 Port forwarding limited to the server

In the router: **Port Forwarding → new rule**.

| Field | Value |
|---|---|
| Protocol | TCP |
| WAN (external) port | a high port of your choice, e.g. `38554`, not the standard 554 |
| LAN host | the camera's IP |
| LAN host port | `554` |
| **Remote host IPs** | **your server's public IP** (the same value as range start and end) |

The "Remote host IPs" field is what matters: it makes the camera unreachable from any other address
on the internet. If your router doesn't allow it, consider a VPN.

Then in the server's `.env`:

```
CAMERA_HOST=your-name.domain.com
CAMERA_PORT=38554
```

### 5.3 Test from the server

```bash
bash -c '</dev/tcp/your-name.domain.com/38554' && echo PORT_OPEN
.venv/bin/python tapo_uploader.py check
```

From outside (for example your phone on 5G) the same port must show as **closed**.

---

## 6. Final check

```bash
cd ~/tapo-uploader
.venv/bin/python tapo_uploader.py test      # records 60 s and uploads, from the server
sudo systemctl start tapo-uploader
systemctl status tapo-uploader              # must be "active (running)"
journalctl -u tapo-uploader -f              # live logs
ls -la recordings                           # the segment being recorded
```

Remember the one-session-per-stream limit: run `check` and `test` **before** starting the service,
or with `CAMERA_STREAM=stream2`.

The first automatic upload happens after the first segment closes (at the next 00, 06, 12 or 18
o'clock) and the file disappears from `recordings/`. On YouTube you find the private video and the
day's playlist. **Private playlists don't show on your channel's public page**: look in
**YouTube → You → Playlists** or in Studio → Content → Playlists.

---

## Motion mode: one video per day

An alternative to continuous recording (`tapo_uploader.py`): `tapo_motion.py` records **only when
the camera detects motion** and uploads **a single video a day**, for example at 21:00. Continuous
recording uploads 4 videos a day and keeps everything; here you keep only what happened.

```
 ffmpeg ──▶ ring of 10 s segments (only the last minute) ──┐
 camera ──ONVIF events──▶ motion? ──▶ clip: 30 s before + 5 min after ──▶ events/YYYYMMDD/
                                                                      │
                  at 21:00: all the day's clips ──▶ one private video ──▶ files deleted
```

**Rules:**
- The first motion opens an event: the clip goes from `MOTION_PRE_SECONDS` (30 s) **before** to
  `MOTION_POST_SECONDS` (5 min) **after**.
- Motion arriving in the **last `MOTION_EXTEND_WITHIN` seconds (30)** of the window extends it by
  another 5 minutes from that moment (for example motion at 4:50). The same if the motion is still
  going on. Motion during the earlier part of the window is ignored.
- At `UPLOAD_AT` (21:00) the day's clips are joined **without re-encoding** into one private video.
  The title is `Tapo 2026-10-26 events 07:12-20:55 (5)` and the **description lists the time of
  every event** (`0:00 Motion at 07:12:03`...): YouTube shows it as chapters. Then the files are
  deleted. If a day had no events nothing is uploaded. An event that ends after 21:00 goes into the
  next video.
- With no motion the disk doesn't fill up: the ring keeps about a minute of video.

**What you also need:**

1. **Detection turned on in the Tapo app** (Settings → Detection & Alarms). Events depend on what
   is on: `IsMotion` = motion detection, `IsPeople` = person detection. Choose with
   `MOTION_EVENTS` (default: both).
2. **ONVIF reachable from the server.** Events go through the camera's port 2020. On the LAN nothing
   is needed; remotely you need **a second port-forwarding rule** like the one in step 5.2: external
   port `38020` (your choice) to `CAMERA_IP:2020`, TCP, **only from the server's IP**. In the
   server's `.env`:

   ```
   ONVIF_HOST=your-name.domain.com
   ONVIF_PORT=38020
   ```

   On some routers (ZTE H388X) the created rule only becomes active after pressing **Apply a second
   time**: check from the server with `bash -c '</dev/tcp/your-name.domain.com/38020'`.
3. **The service in place of the continuous one.** The camera accepts one RTSP session per stream:
   stop one before starting the other.

   ```bash
   sudo systemctl disable --now tapo-uploader
   sudo cp deploy/tapo-motion.service /etc/systemd/system/    # set USER and the paths
   sudo systemctl daemon-reload && sudo systemctl enable --now tapo-motion
   journalctl -u tapo-motion -f
   ```

**Commands:**

```bash
python tapo_motion.py listen 60    # prints the motion events the camera sends (ONVIF test)
python tapo_motion.py run          # records around motion, uploads once a day
python tapo_motion.py upload       # joins and uploads the pending clips right now
```

**Quick test with short times**, from a PC on the LAN (use `stream2` if the service holds
`stream1`):

```bash
CAMERA_STREAM=stream2 MOTION_POST_SECONDS=20 MOTION_EXTEND_WITHIN=10 UPLOAD_AT=00:00 \
  python tapo_motion.py run
```

Walk in front of the camera: the log shows `motion: recording...`, then `clip saved` and, within a
minute, the upload of the day's video (`UPLOAD_AT=00:00` makes it due immediately).

**Good to know:**
- The 30 seconds of pre-roll have a tolerance of about 10 seconds (10 s segments, cuts on
  keyframes).
- If the service restarts during an event, that clip is lost.
- With a still camera `listen` prints nothing: the camera only reports state changes.

---

## Multi-account mode

An **optional** mode for a group of trusted people (friends, family) on one server. Each person signs in, links
**their own camera** and **their own YouTube channel**, and sees only their own videos and camera status on a
personal page. Nothing changes for single-user setups: without `MULTIUSER=1` the programs above work exactly as
before, with the same `.env`.

What it adds: a small web app (`python -m tapo_web run`) with a SQLite database, a supervisor that runs one
recorder per camera, a router guide for each friend (manual steps and a text to paste into an AI assistant),
a "Test connection" button, and an administrator page. Sign-in is configurable: local accounts, and/or
**Nextcloud** (its groups become roles). Passwords and tokens are stored encrypted.

> **Quota, read this first.** All users share the Google project's YouTube quota (10,000 units a day by default).
> One full daily upload costs about 1,700 units, so **one project serves about 5 or 6 users** at one upload a day.
> Ways around it: ask Google for a higher quota, let some users upload every other day, or enable
> `ALLOW_OWN_GOOGLE_CLIENT` so a user brings their own Google project. The admin page shows the quota used today.

### Setup

1. **Install** on the server: copy the project (including the `tapo_web` folder), then
   `python3 -m venv .venv && .venv/bin/pip install -r requirements-web.txt`.
2. **Google project.** In Google Cloud create an OAuth client of type **Web application** (not Desktop) with the
   authorized redirect URI `https://YOUR-ADDRESS/youtube/callback`. Put its JSON in `YOUTUBE_CLIENT_SECRET_JSON`.
   The app must be *In production* (step 2.4 above); beyond 100 users Google requires verification. Other
   people will now use your app: update the consent screen and the privacy policy page to say so.
3. **Keys and settings.** Copy the multi-account block of `.env.example` and fill it in. Generate the two keys:
   `python -c "from tapo_web.crypto import Vault; print(Vault.generate_key())"` for `ENCRYPTION_KEY`, and any long
   random string for `SECRET_KEY`. Keep `ENCRYPTION_KEY` backed up: without it the stored passwords and tokens
   cannot be read. Set `PUBLIC_URL` (HTTPS) and `SERVER_PUBLIC_IP` (the address friends will allow in their routers).
4. **First administrator.** `python -m tapo_web create-admin`, or set `ADMIN_USERNAME` and `ADMIN_PASSWORD` for the
   first start.
5. **Nextcloud (optional).** In Nextcloud, Settings, Security, *OAuth 2.0 clients*: add a client with the redirect
   URI `https://YOUR-ADDRESS/auth/nextcloud/callback`, then set `NEXTCLOUD_URL`, `NEXTCLOUD_CLIENT_ID` and
   `NEXTCLOUD_CLIENT_SECRET`. Roles come from groups: members of `NEXTCLOUD_ADMIN_GROUP` (default `admin`) are
   administrators, members of `NEXTCLOUD_USER_GROUP` may use the service. If that group is not set, only
   administrators can sign in. Roles are re-checked at every login: someone removed from the group is locked out at
   their next login, and stays out until an administrator reactivates them.
6. **Run it** behind an HTTPS reverse proxy (nginx, Caddy, aaPanel): `python -m tapo_web run` (port `PORT`, default
   8080), or as a service with `deploy/tapo-web.service`, or in Docker with
   `CMD ["python", "-u", "-m", "tapo_web", "run"]`. Behind one reverse proxy set `TRUSTED_PROXY_HOPS=1`, otherwise every visitor looks like the proxy and the login attempt limits cannot tell them apart. It is not meant for Render's free plan (ephemeral disk).

### Adding a friend's camera

The friend signs in, then **Connect YouTube** (their own channel), then **Add a camera** (their dynamic DNS name,
the camera account from the Tapo app). The server assigns two random external ports and shows a router guide:
open those two ports **only for `SERVER_PUBLIC_IP`**. **Test connection** checks the port, the video stream and the
motion events; only then does recording start.

### Good to know

- Camera addresses must be public: private, loopback and link-local addresses are refused so nobody can use the
  server to reach its own network. Set `ALLOW_PRIVATE_HOSTS=1` only on a home network.
- One camera per user by default (`MAX_CAMERAS_PER_USER`), 20 in total (`MAX_CAMERAS_TOTAL`).
- Deleting a camera also deletes its recordings on the server.
- Uploads of different cameras are spread over time (`UPLOAD_AT`, then `UPLOAD_STAGGER_MINUTES` apart).
- A deactivated user's data and tokens are deleted after `RETENTION_DAYS` (30). If someone's YouTube link is
  missing or revoked, their clips are kept for 7 days and then discarded.
- Every setting is listed in `.env.example`.

---

## Where the recordings go

By default the daily video goes to YouTube. You can send it somewhere else:

| Destination | `STORAGE` | What it needs |
|---|---|---|
| YouTube (default) | `youtube` | the Google setup above |
| WebDAV: Nextcloud, ownCloud, Synology... | `webdav` | `WEBDAV_URL`, `WEBDAV_USER`, `WEBDAV_PASS` (for Nextcloud with two-factor, an app password), optional `WEBDAV_FOLDER` |
| A folder on the machine running the program | `local` | `LOCAL_ROOT` |
| Anything rclone supports: Mega, S3-compatible storage (Backblaze B2, Wasabi, MinIO), Dropbox, OneDrive... | `rclone` | the `rclone` program, a remote created with `rclone config`, `RCLONE_REMOTE` (optional `RCLONE_CONFIG`, `RCLONE_FOLDER`) |

Set the variables in `.env` (they are all listed in `.env.example`) and restart. The videos are stored as
`<folder>/Tapo YYYY-MM-DD/<title>.mp4`, with the chapters in a `.chapters.txt` file next to each. A file is written
under a temporary name and renamed when it is complete, so an interrupted upload never looks finished, and the
clips stay on the machine until the upload succeeds. This applies to the motion mode (`tapo_motion.py`); the
continuous mode (`tapo_uploader.py`) always uses YouTube.

**Mega** works through rclone's `mega` backend. The author has **not tested it**: Mega offers no official API for
this, so read rclone's documentation for its limits before relying on it.

**In multi-account mode** the server chooses the default (`STORAGE` and the `STORAGE_*` variables) and every user
gets it without doing anything, even without signing in with Nextcloud. Destinations that live on the server
(WebDAV, folder, rclone) get one sub-folder per camera, so nobody can see another person's videos. With
`ALLOW_OWN_STORAGE=1` each user can pick, on the **Recordings** page, YouTube or their own WebDAV
(`STORAGE_ALLOWED` narrows the choice; the address must be public and `https`). `local` and `rclone` are
server-only on purpose: a path or an rclone configuration chosen by a user would give them access to the server
itself. If a saved choice stops being allowed, the server default is used and the page says so. Only YouTube
spends the shared quota. With a server-hosted destination the **server stores the footage**: plan the disk space
and who can read it.

**Watching:** in multi-account mode the personal page shows every video as a card with two actions. **Watch here**
opens a player on the page (YouTube's embedded player, or the app itself streaming the file, with seeking, only to
its owner). **Open the video** goes to the video on YouTube, or to the file in your own cloud (nothing for a local
folder or rclone). The audio is converted to AAC while recording and the video is copied as it comes: keep the
camera on H.264, because H.265 files may not play in some browsers.

---

## Daily use

```bash
sudo systemctl status tapo-uploader       # status
journalctl -u tapo-uploader -f            # logs
sudo systemctl restart tapo-uploader      # after changing .env or the script
sudo systemctl stop tapo-uploader         # stop it
ls -la ~/tapo-uploader/recordings         # segments waiting or being recorded
```

(Use `tapo-motion` instead of `tapo-uploader` in motion mode.)

- A restart closes the current segment and opens a new one, so a shorter video is uploaded too.
- Segments are "fragmented" MP4s: they stay playable even if ffmpeg is killed abruptly (reboot,
  power off). At most the last few seconds are lost.

---

## Configuration

`.env` file (from `.env.example`, **never** in the repository):

| Variable | Meaning |
|---|---|
| `CAMERA_USER`, `CAMERA_PASS` | Camera account created in the Tapo app (not the cloud account). |
| `CAMERA_HOST`, `CAMERA_PORT` | On the LAN the camera's IP and `554`. Remotely the DDNS name and external port. |
| `CAMERA_STREAM` | `stream1` (high quality) or `stream2` (low). |
| `SEGMENT_SECONDS` | Length of each file, default `21600` (6 hours). |
| `RECORD_DIR` | Folder of files waiting for upload, default `recordings`. |
| `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_TOKEN` | Names of the `client_secret.json` and `token.json` files. |
| `YOUTUBE_PRIVACY` | `private` (default), `unlisted` or `public`. |
| `PLAYLIST_PREFIX` | Prefix of titles and playlists (default `Tapo`). |
| `DATA_DIR` | Folder for token, state and pending videos (default: the project folder). In a container it is the persistent disk, e.g. `/data`. |
| `YOUTUBE_TOKEN_JSON`, `YOUTUBE_CLIENT_SECRET_JSON` | Contents of `token.json` and `client_secret.json`, for containers: if the files are missing they are created from these. |

Only for `tapo_motion.py` (defaults in brackets):

| Variable | Meaning |
|---|---|
| `MOTION_PRE_SECONDS` (30) | Seconds recorded before the first motion. |
| `MOTION_POST_SECONDS` (300) | Seconds recorded after the motion. |
| `MOTION_EXTEND_WITHIN` (30) | Motion in the last N seconds of the window extends it. |
| `MOTION_EVENTS` (`IsMotion,IsPeople`) | ONVIF events that count as motion. |
| `UPLOAD_AT` (`21:00`) | Time after which the day's video is uploaded. |
| `BUFFER_SEGMENT_SECONDS` (10) | Length of the ring's segments. |
| `BUFFER_DIR`, `EVENTS_DIR` | Folders of the ring and of the clips (`buffer`, `events`). |
| `ONVIF_HOST`, `ONVIF_PORT` (camera host, `2020`) | Where to reach the ONVIF events. |

---

## Under the hood

- **Wall-clock-aligned segments.** `-segment_atclocktime 1` cuts at 00/06/12/18; the cut happens at
  the next keyframe, so durations vary by a few seconds.
- **Timestamps.** The camera sends H.264 packets without valid timestamps: ffmpeg uses
  `-use_wallclock_as_timestamps 1 -fflags +genpts`.
- **Audio.** The camera's audio is PCM A-law, which MP4 doesn't accept: it is converted to AAC
  (64 kbit/s, mono). The video is untouched.
- **Fragmented MP4.** `movflags=+frag_keyframe+empty_moov+default_base_moof`. A normal MP4 writes
  its index (`moov`) only at the end: if ffmpeg is killed the file is unreadable. With
  fragmentation each piece is already complete.
- **Title.** The start comes from the file name, the end from the last write: right even for
  partial segments.
- **Upload.** Resumable in 8 MB chunks. Only what has been still for more than 2 minutes is
  uploaded. If the YouTube quota is used up, it waits an hour.
- **Playlist.** It is searched by title among yours; if missing it is created. The ID is kept in
  `state.json`.
- **ONVIF events.** A PullPoint subscription on the camera's port 2020 with WS-Security
  (UsernameToken digest), polling in 1-second requests over a plain socket, renewed every 90 s.
  Only state changes are forwarded.
- **Motion engine.** A ring of 10-second segments keeps the pre-roll; on an event the needed
  segments are joined with ffmpeg `concat` (copy) into one clip; the day's clips are joined the
  same way into the video that is uploaded.
- **Clean logs.** The camera credentials are always masked (`rtsp://***@...`), because ffmpeg
  prints the full URL in errors. ffmpeg only logs errors.

---

## YouTube quota

Each upload costs about 1,650 units (1,600 the video, 50 the playlist insert) and the default daily
quota is **10,000**: about **6 videos a day**.

| Segment length | Uploads per day | Within quota? |
|---|---|---|
| 6 hours (default) | 4 | yes |
| 4 hours | 6 | at the limit |
| 1 hour | 24 | no, you must ask Google for an increase |
| Motion mode | 1 | yes |

If the quota runs out the script writes it in the log and retries after an hour. The quota resets
at midnight Pacific Time.

---

## Security

- Never publish `.env`, `client_secret.json`, `token.json`. `.gitignore` excludes them.
- `token.json` gives access to your YouTube channel: mode `600`. If lost, revoke access at
  <https://myaccount.google.com/permissions>.
- Limit port forwarding to the server's IP and use a non-standard external port.
- Use **different** passwords for the camera, the Tapo account and the server. Don't reuse them and
  don't paste them in chats or tickets.
- If the server's IP changes, update the router rule.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `401 Unauthorized` | Wrong username or password of the **camera account** (not the Tapo cloud ones). |
| `Operation not permitted` | Another session is already using that stream: stop the service or use `stream2`. |
| Timeout from the server | Missing port forwarding, server IP changed, home IP changed and DDNS not updated, or CGNAT. |
| `ensurepip is not available` creating the venv | `python3-venv` is missing: `sudo apt-get install python3-venv`. |
| `invalid_grant` on YouTube | Token expired or revoked (typical if the app was in Testing): run `auth` again and copy `token.json`. |
| The "Publish app" button is grey | Home page, privacy policy or authorized domain missing in Branding. |
| `moov atom not found` on a file | Segment written by a version without fragmented MP4 and interrupted: unrecoverable, delete it. |
| Playlist with the wrong day or cuts at 1 AM | The process runs on UTC: `TZ` is missing in the service. |
| I can't see the playlist in my channel | It is private: look in YouTube → You → Playlists, not on the channel's public page. |
| No upload after hours | The segment is not closed yet: look at `recordings/` and `journalctl -u tapo-uploader`. |
| `listen` never prints anything | Detection is off in the Tapo app, or there is no motion: events arrive only on state changes. |
| ONVIF events: timeout or no answer | The ONVIF port is unreachable (rule to re-apply, server IP changed). Try `bash -c '</dev/tcp/HOST/PORT'`. |
| The camera answers only the first ONVIF request | Happens with long `PullMessages` or standard HTTP clients: the script uses 1 s requests over a plain socket. |
| Events always ignored | `MOTION_EVENTS` doesn't contain the event the camera sends (`IsPeople` instead of `IsMotion`, or vice versa). |
| `quotaExceeded` | Too many uploads for the daily quota: lengthen the segments. |

---

## FAQ

**Can a Tapo C200 upload to YouTube?**
Not by itself. The camera has no YouTube or Google Drive option. This project adds it from outside:
a small service reads the camera's RTSP stream and uploads with the official YouTube API.

**Do I need to flash custom firmware or root the camera?**
No. The stock firmware already exposes RTSP and ONVIF once you create the *Camera Account* in the
Tapo app. Nothing on the camera is modified.

**Is this a free alternative to Tapo Care or a cloud subscription?**
It replaces the cloud storage part: footage goes to your own YouTube channel, which is free. You
lose the app's cloud timeline, so keep Tapo Care if you rely on it. Running the service costs
nothing on a Raspberry Pi you already own, and a few dollars a month on a VPS.

**Will the videos be public?**
No. They are uploaded as `private` and only your Google account can see them. You can change it to
`unlisted` or `public` with `YOUTUBE_PRIVACY`.

**Does it record all day or only on motion?**
Your choice. `tapo_motion.py` records only around motion detection (30 s before, 5 min after) and
uploads one video a day. `tapo_uploader.py` records continuously in segments.

**How do I record a Tapo camera on motion only?**
Use `tapo_motion.py`: it subscribes to the camera's ONVIF motion and person-detection events and
keeps a short ring buffer so the seconds *before* the trigger are included. See
[Motion mode](#motion-mode-one-video-per-day).

**Does it work with other TP-Link Tapo cameras or other brands?**
Only the Tapo C200 has been tested. Any camera that offers RTSP and ONVIF event notifications has a
good chance, with different stream paths or event names. Issues and pull requests with other models
are welcome.

**Will I hit the YouTube API quota?**
No. One upload costs about 1,650 of the 10,000 free daily units. Motion mode uses one upload a
day; continuous mode with 6-hour segments uses four.

**Do I need to expose my camera to the internet?**
Only if the server is not on your home network. At home (Raspberry Pi, mini PC) nothing is
exposed. With a remote VPS you forward one or two ports restricted to the server's IP; see
[step 5](#5-reaching-the-camera-from-the-internet) and [Security](#security).

**Can it run on Docker, Render or a Raspberry Pi?**
Yes to all three. There's a `Dockerfile`, Render blueprints and a systemd unit. See
[Where to run it](#where-to-run-it) for the honest trade-offs of each.

**Why not Home Assistant, Frigate or Scrypted?**
They are great and much bigger. This is a single small script for one job: get motion footage into
YouTube without running a home-automation stack.

---

## Contributing

Issues and pull requests are welcome, especially: other camera models, router guides for port
forwarding, a notification on failures, and a tested Render or Fly.io setup. If this saved you a
subscription, a ⭐ helps other people find it.

## License

MIT, see [LICENSE](LICENSE).
