# Pluggable storage destinations — design

Date: 2026-10-26. Status: draft for approval. Builds on `2026-10-26-multi-account-design.md`.

## Intent

Today every recording ends up on YouTube. Some people do not want that (privacy, no channel, YouTube's
10,000-unit daily quota, a Reddit reader asked for Mega). Make the destination a choice: the server picks a
default, and when accounts are enabled each account may override it. The pipeline before the upload
(RTSP recording, ONVIF motion, daily join with chapters) does not change.

## Goals

- One small storage interface; the uploader stops knowing about YouTube.
- Destinations in v1: **YouTube** (existing behaviour, unchanged), **WebDAV** (Nextcloud, ownCloud, Synology, any WebDAV),
  **local folder**, **rclone** (any rclone remote: Mega, S3-compatible, Dropbox, OneDrive, Drive, ...).
- Server-level default, usable in single-user mode and with no login at all.
- Per-account override in multi-account mode, only where the admin allows it.
- Existing installs keep working with zero configuration changes (default stays YouTube).

## Non-goals / anti-goals

- No uploading to several destinations at once for the same camera (one destination per account/server).
- No transcoding of video. The server may stream a file to its owner's browser (see the player), nothing more.
- No native Mega client: Mega has no official Python SDK, so Mega goes through rclone (its `mega` backend).
- Anti-goal: a friend must never be able to make the server run arbitrary commands, read its files, or reach
  its private network by choosing a destination.

## Decisions (made by the author of this document, not yet confirmed by the user)

1. **Interface** (`tapo_storage/base.py`): `Storage` with `check() -> None` (raises with a readable message),
   `put(path, day, name, description) -> StoredItem`, `list_day(day) -> list[StoredItem]` and `open(item, start, end) -> iterator of bytes` (both optional,
   `browsable = False` by default). `StoredItem` = name, size, url-or-None, when. A factory
   `make_storage(kind, config, ctx)` builds one.
2. **Layout** on file-like destinations: `<prefix> YYYY-MM-DD/<name>.mp4`, mirroring the YouTube playlist
   "Tapo YYYY-MM-DD". The chapters that YouTube gets in the description are written next to the file as
   `<name>.chapters.txt`.
3. **WebDAV** uses plain HTTP (stdlib or `requests`, already in the web requirements): `MKCOL` for the day folder,
   `PUT` streaming the file, `PROPFIND` for listing. In multi-account mode the URL passes the same SSRF guard as
   camera hosts (`assert_public_host`), unless the admin sets `ALLOW_PRIVATE_HOSTS` (a Nextcloud on the same LAN).
4. **Local folder** writes under the camera's data dir or an admin-set `STORAGE_LOCAL_ROOT`.
5. **rclone** runs the `rclone` binary with a remote name and `--config` pointing at a server-side file the admin
   controls. The Docker image installs rclone; elsewhere it is optional and `check()` says if it is missing.
6. **Per-user override is limited to YouTube and WebDAV.** `local` and `rclone` can only be chosen at server
   level, because a user-supplied rclone config or path is command execution / file access on the server.
7. **Resolution order:** account override (if `ALLOW_OWN_STORAGE=1` and the user saved one) → server default
   (`STORAGE=` env, default `youtube`) . `STORAGE_ALLOWED` (comma list, default `youtube,webdav`) limits what
   users may pick.
8. **Storage of settings:** table `storage_configs(user_id, kind, config_encrypted)`, encrypted with the existing
   `Vault`. YouTube keeps its own OAuth link table; choosing `youtube` just means "use the link". Schema
   migration bumps `schema_version`.
9. **Quota:** only YouTube goes through `QuotaLedger`/`UploadGate`/stagger. Other kinds bypass the gate.
10. **Player on the personal page.** YouTube: embedded `youtube-nocookie.com` iframe; the CSP gains
    `frame-src https://www.youtube-nocookie.com` and nothing else. WebDAV, local, rclone: a `<video>` tag pointing at
    `/media/<camera_id>/<item_id>`, served by the app with HTTP Range support (WebDAV: forward the Range request with
    the vault credentials; local: `send_file` conditional; rclone: `rclone cat --offset/--count`). The CSP gains
    `media-src 'self'`. Every request checks the session and that the camera belongs to the user (admins excepted);
    item ids are database/listing ids, never paths. All four kinds are therefore browsable.
10b. **Audio for browsers.** Tapo audio is often G.711, which browsers do not play. For non-YouTube destinations the
    uploader remuxes with `-c:v copy -c:a aac` when the file has a non-AAC audio track (first test of the plan:
    check the codec of real files). YouTube keeps receiving the file untouched.
11. **Failure behaviour** is the same as today: a failed upload keeps the clip, reports status, retries with
    backoff; a wrong setting is caught earlier by the "Test" button calling `check()`.
12. **Single-user env:** `STORAGE`, `WEBDAV_URL`, `WEBDAV_USER`, `WEBDAV_PASS`, `LOCAL_ROOT`, `RCLONE_REMOTE`,
    `RCLONE_CONFIG`. Unset means YouTube, exactly as now. Multi-account server defaults reuse the same names with
    the `STORAGE_` prefix where they would clash.

## Parts left to the builder

Module layout beyond the interface, exact form fields and copy, chunking of the WebDAV PUT, how `check()` tests
write access (create and remove a probe file), test fakes for each backend, README/index wording.

## Review focus (inputs likely to bite)

- WebDAV server that returns 301/302 on `PUT` (HTTP→HTTPS): must not silently drop the body or follow into a
  private address.
- Names with spaces, `#`, `?`, non-ASCII in WebDAV/rclone paths.
- Upload interrupted halfway: the clip must stay local and the partial remote file must not be taken as done
  (write to a temp name, rename on success where the destination supports it).
- Changing destination while clips are pending: pending clips go to the new destination, never lost.
- Admin removes a kind from `STORAGE_ALLOWED` while users have it saved: they fall back to the server default,
  with a visible notice.
- rclone not installed / remote missing: clear error in the UI, not a crash loop.
- `/media` with `../`, another user's ids, a huge or invalid Range header, or a WebDAV file that vanished: 403/404/416, no
  traversal, no server-side buffering of the whole file.
- Video with no audio, or audio the browser cannot decode: still plays, and the page says so instead of failing silently.

## Rollout

New branch `feature/storage-backends`; plan via writing-plans; TDD; docs (README EN/IT) and the landing page get a
"Choose where recordings go" section when it ships.
