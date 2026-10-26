# Pluggable Storage Destinations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recordings can go to YouTube, WebDAV, a local folder or an rclone remote; the server picks a default, accounts may override it, and the personal page plays the videos ("Guarda qui") or links to them ("Vai al video").

**Architecture:** A `tapo_storage` package (repo root, next to `camera_context.py`) defines a `Storage` interface and four backends. `CameraContext` gets an optional `storage`; the daily uploader in `tapo_motion` talks only to it (YouTube becomes one backend that wraps today's code). The web app resolves the destination per user (override → server default), stores overrides encrypted, and serves videos through a Range-capable `/media` route.

**Tech Stack:** Python stdlib (`http.client`, `subprocess`, `xml.etree`) for WebDAV/rclone/ffmpeg, Flask, SQLite, pytest. No new runtime dependency. `rclone` is an optional binary (Dockerfile installs it).

**Spec:** `docs/superpowers/specs/2026-10-26-storage-backends-design.md`. Builds on the multi-account code (`tapo_web/*`, 218 tests passing at start).

## Global Constraints

- Single-user mode with no new env var must behave exactly as today: default destination is YouTube, `requirements.txt` keeps its 3 packages, `tests/test_characterization.py` and `tests/test_single_user_regression.py` stay green after every task.
- Only YouTube goes through `QuotaLedger`/`UploadGate`/stagger; other kinds bypass the gate.
- Users may choose only `youtube` and `webdav`; `local` and `rclone` are server-level only. `STORAGE_ALLOWED` default `youtube,webdav`; `ALLOW_OWN_STORAGE` default off.
- WebDAV URLs from users pass `assert_public_host` (unless `ALLOW_PRIVATE_HOSTS`); the connection goes to the checked IP; redirects are never followed.
- Remote file names come from `safe_filename(title)`: letters, digits, space, `.`, `_`, `-`, `(`, `)`, never `/`, `\`, `:` or control characters, always ending `.mp4`; day folders are `"<playlist_prefix> YYYY-MM-DD"`.
- Continuous (non-motion) recording mode in `tapo_uploader.upload_loop` stays YouTube-only.
- No inline scripts/styles, no external assets: CSP becomes `default-src 'self'; img-src 'self' https://i.ytimg.com data:; frame-src https://www.youtube-nocookie.com; media-src 'self'`.
- Public repo: commit author is the GitHub noreply address; never commit `.env`, tokens, recordings; scan for the author's private strings (kept outside the repository) before any push. Commit trailers: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01HbHpACkCoxHhuDEuy2FdgF`.
- Test command for every task: `python -m pytest tests -q` in the clone's venv.

## Review Focus

1. WebDAV answers 301/302 to `PUT`: fail with a readable error, never retry the body elsewhere (Task 3).
2. Names with spaces, `#`, `?`, non-ASCII in WebDAV/rclone paths (Tasks 1, 3, 4).
3. Upload interrupted halfway: the local clip stays, no half-written remote file is mistaken for done (Tasks 3, 4, 5).
4. Destination changed while clips are pending: they go to the new destination (Task 8).
5. Admin removes a kind from `STORAGE_ALLOWED` while users saved it: fall back to server default with a notice (Tasks 8, 9).
6. rclone missing or remote wrong: UI error, not a crash loop (Tasks 4, 9).
7. `/media` abuse: `../`, other user's camera, invalid/huge Range, vanished file (Task 10).
8. Video with audio browsers cannot decode (Task 5) and with no audio at all.

---

### Task 1: Storage interface, file naming, local backend

**Files:**
- Create: `tapo_storage/__init__.py`, `tapo_storage/base.py`, `tapo_storage/local.py`
- Test: `tests/test_storage_base.py`

**Interfaces:**
- Produces (`tapo_storage.base`): `class StorageError(Exception)`; `@dataclass(frozen=True) class StoredItem: name: str; day: str; size: Optional[int]; modified: str; url: Optional[str]`; `safe_filename(title: str) -> str`; `day_folder(prefix: str, day: str) -> str`; `class Storage` with class attributes `kind: str`, `uses_quota = False`, `browsable = True`, `needs_browser_audio = True` and methods `check() -> None`, `put(path: Path, day: str, title: str, description: str = "") -> StoredItem`, `days(limit: int = 10) -> list[str]` (newest first, `YYYY-MM-DD`), `list_day(day: str) -> list[StoredItem]`, `open(day: str, name: str, start: int = 0, end: Optional[int] = None) -> tuple[Iterator[bytes], int]` (chunks plus total size; `end` inclusive); `make_storage(kind: str, config: dict, ctx, host_check=None) -> Storage` (registry; unknown kind → `StorageError`).
- Produces (`tapo_storage.local`): `LocalStorage(root: Path, prefix: str)` implementing all methods; files written to `<name>.part` then `os.replace`d.

- [ ] **Step 1: Write failing tests** in `tests/test_storage_base.py`: `test_safe_filename_strips_path_and_forbidden_characters` (`"Tapo 2026-10-26 eventi 12:30-13:10 (3)"` → `"Tapo 2026-10-26 eventi 12-30-13-10 (3).mp4"`, `"../../x/é#?.mp4"` has no `/`, `\`, `#`, `?` and ends `.mp4`, empty title → `"video.mp4"`); `test_local_put_writes_file_and_chapters_sidecar` (put returns `StoredItem`, file in `root/"Tapo 2026-10-26"/name`, `name.chapters.txt` holds the description, no `.part` left); `test_local_put_leaves_no_final_file_when_copy_fails` (monkeypatch copy to raise → `StorageError`, no `.mp4` in folder); `test_local_days_and_list_day_newest_first`; `test_local_open_honours_range` (`start=2,end=5` returns bytes 2..5 and total size); `test_local_check_writes_and_removes_probe`; `test_make_storage_rejects_unknown_kind`.
- [ ] **Step 2: Run** `python -m pytest tests/test_storage_base.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the interface and `LocalStorage` as specified; `list_day` skips `.part` and sidecars; `open` reads in 1 MiB chunks.
- [ ] **Step 4: Run** the file, then the full suite. Expected: all pass.
- [ ] **Step 5: Commit** `feat: storage interface and local backend`.

### Task 2: YouTube backend and uploader refactor

**Files:**
- Create: `tapo_storage/youtube.py`
- Modify: `camera_context.py` (new field `storage: Optional[object] = None`), `tapo_motion.py` (`upload_day`, `upload_pending`, `daily_loop`), `tapo_storage/base.py` (register `youtube`)
- Test: `tests/test_storage_youtube.py` (plus existing characterization tests must stay green)

**Interfaces:**
- Consumes: Task 1 `Storage`, `StorageError`, `StoredItem`; existing `tapo_uploader.upload(ctx, yt, state, path, start, title, description)`, `load_state`, `youtube_client`, `is_quota_error`; `tapo_web.dashboard.list_playlists` stays where it is.
- Produces: `YouTubeStorage(ctx)` with `kind="youtube"`, `uses_quota=True`, `needs_browser_audio=False`, `check()` (obtains the client, raises `StorageError` on failure), `put(path, day, title, description)` (calls `tapo_uploader.upload` with `start=datetime.strptime(day, "%Y-%m-%d")`, keeps its own `state`), `browsable=False` for the interface's list methods (the web layer keeps using `list_playlists`; items carry `url=https://www.youtube.com/watch?v=<id>`); `tapo_motion.storage_for(ctx) -> Storage` returns `ctx.storage` or `YouTubeStorage(ctx)`.

- [ ] **Step 1: Write failing tests**: `test_youtube_put_delegates_to_base_upload_with_day_start` (monkeypatch `tapo_uploader.upload`, assert args), `test_storage_for_defaults_to_youtube`, `test_storage_for_uses_context_storage`, `test_upload_pending_uses_storage_put_and_skips_gate_when_not_quota` (fake storage with `uses_quota=False`, fake gate that fails if called), `test_daily_loop_reports_storage_error_without_quota_handling` (fake storage raising `StorageError("boom")` → `report("error", "boom")`, loop keeps running).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** In `tapo_motion`: `upload_pending` builds `storage = storage_for(ctx)`, calls `storage.put(day_file, f"{first:%Y-%m-%d}", title, description)` from `upload_day(ctx, storage, day_dir, report)`; the gate is consulted and charged only when `storage.uses_quota`; for `needs_browser_audio` storages `upload_day` passes the joined file through `prepare_for_browser` (stub returning the same path until Task 5); `daily_loop` also catches `StorageError` and reports `ctx.scrub(str(err))`. `cmd_run` keeps its fail-fast `base.youtube_client(ctx)` only when `storage_for(ctx).kind == "youtube"`.
- [ ] **Step 4: Run** full suite. Expected: all pass, including the 218 earlier tests.
- [ ] **Step 5: Commit** `refactor: daily uploads go through a storage object`.

### Task 3: WebDAV backend

**Files:**
- Create: `tapo_storage/webdav.py`, `tests/fakes.py` (a threaded in-process fake WebDAV server)
- Test: `tests/test_storage_webdav.py`

**Interfaces:**
- Consumes: Task 1 interface; `tapo_web.security.checked_host` is NOT imported here: the factory passes `host_check: Optional[Callable[[str], str]]` that returns the IP to connect to.
- Produces: `WebDAVStorage(url: str, user: str, password: str, prefix: str, folder: str = "", host_check=None, timeout: int = 60)` (`kind="webdav"`, `browsable=True`); fake server in `tests/fakes.py`: `FakeDAV(mode="ok"|"redirect"|"drop_on_put", users=...)` with `.url`, `.files: dict[path, bytes]`, supporting `MKCOL`, `PUT`, `MOVE`, `DELETE`, `PROPFIND` (Depth 1), `GET` with `Range`, Basic auth.

- [ ] **Step 1: Write failing tests**: `test_check_creates_and_removes_probe`; `test_check_reports_wrong_password` (401 → `StorageError` mentioning "login"); `test_put_makes_day_folder_uploads_part_then_moves` (final file present, no `.part`); `test_put_with_spaces_hash_question_and_unicode_in_title` (stored under percent-encoded path, listing returns the original name); `test_put_does_not_follow_redirect` (mode redirect → `StorageError` mentioning "redirect", fake's second host never contacted, body not re-sent); `test_interrupted_put_leaves_no_final_file` (mode drop_on_put closes the socket mid-body → `StorageError`, no final file, local source untouched); `test_put_streams_without_loading_file_in_memory` (assert chunked reads: wrap file object, max single read ≤ 1 MiB); `test_days_and_list_day_via_propfind` (returns newest first, sizes, `modified`, `url` = the file's URL); `test_open_forwards_range_and_returns_total` (206 body slice); `test_host_check_result_is_the_address_connected_to` (host_check returns `127.0.0.1` for name `dav.example.test`, request still carries `Host: dav.example.test`); `test_private_host_check_failure_propagates_as_storage_error`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement with `http.client` (HTTPS via `HTTPSConnection` with the original hostname as `server_hostname`, HTTP otherwise).** `put`: `MKCOL` each missing path segment (405/409-exists is fine), `PUT` `<name>.part` with `Content-Length` and a file object, then `MOVE` to the final name with `Overwrite: T`; also `PUT` the sidecar when `description` is not empty. Any 3xx → `StorageError("the server redirected the upload (HTTP 301): use the final https:// address")`. Parse `PROPFIND` with `xml.etree` (namespace `DAV:`), ignore `.part` and sidecars. Register in the factory (`config` keys `url`, `user`, `password`, `folder`).
- [ ] **Step 4: Run** the file and the full suite. Expected: pass.
- [ ] **Step 5: Commit** `feat: WebDAV storage backend`.

### Task 4: rclone backend

**Files:**
- Create: `tapo_storage/rclone.py`
- Test: `tests/test_storage_rclone.py`

**Interfaces:**
- Produces: `RcloneStorage(remote: str, config: Optional[Path], prefix: str, run=subprocess.run, binary="rclone")` (`kind="rclone"`, `browsable=True`, `url=None` on items); every call is a list of arguments (no shell); `check()` runs `lsd <remote>:` and raises `StorageError("rclone is not installed")` on `FileNotFoundError`, `StorageError("the rclone remote 'x' is not usable: <first stderr line>")` on non-zero exit; `put` runs `copyto <file> "<remote>:<folder>/<name>.part"` then `moveto` to the final name; `days`/`list_day` use `lsjson`; `open` uses `cat --offset <start> --count <n>` and `size` from `lsjson`.

- [ ] **Step 1: Write failing tests** with a fake `run` recording argument lists: `test_check_reports_missing_binary`, `test_check_reports_bad_remote`, `test_put_copies_to_part_then_moves_and_never_uses_a_shell`, `test_put_failure_keeps_no_final_name` (copy fails → `StorageError`, no `moveto` call), `test_names_with_spaces_hash_and_unicode_are_passed_as_single_arguments`, `test_list_day_parses_lsjson_and_skips_part_files`, `test_open_uses_offset_and_count`, `test_config_file_is_passed_with_dash_dash_config`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** as specified; remote and folder are validated (`^[A-Za-z0-9_.-]+$` for the remote name; folder may not contain `..`). Register in the factory (`config` keys `remote`, `config`, `folder`).
- [ ] **Step 4: Run** the file and the full suite. Expected: pass.
- [ ] **Step 5: Commit** `feat: rclone storage backend`.

### Task 5: Browser-friendly audio

**Files:**
- Create: `tapo_storage/audio.py`
- Modify: `tapo_motion.py` (replace the Task 2 stub)
- Test: `tests/test_storage_audio.py`

**Interfaces:**
- Produces: `audio_codec(path: Path, run=subprocess.run) -> Optional[str]` (first audio stream's codec via `ffprobe`, `None` if no audio); `prepare_for_browser(path: Path, run=subprocess.run) -> Path` (returns `path` untouched when the audio is `aac` or absent; otherwise writes `<stem>.aac.mp4` next to it with `-c:v copy -c:a aac -b:a 64k -movflags +faststart`, deletes the original and returns the new path; on ffmpeg failure raises `StorageError` and keeps the original).

- [ ] **Step 1: Write failing tests** with a fake `run`: `test_aac_audio_is_left_alone`, `test_no_audio_is_left_alone`, `test_pcm_alaw_is_remuxed_to_aac_copying_video`, `test_remux_failure_keeps_original_and_raises`; and one real-ffmpeg test `test_real_g711_clip_becomes_aac` generated with `ffmpeg -f lavfi -i testsrc -f lavfi -i sine -c:v libx264 -c:a pcm_alaw` (skipped when `ffmpeg` is not on PATH).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement**; in `upload_day` call `prepare_for_browser(day_file)` only when `storage.needs_browser_audio`, and keep deleting the resulting file in the clean-up.
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `feat: convert non-AAC audio for browser playback`.

### Task 6: Single-user configuration from the environment

**Files:**
- Modify: `camera_context.py` (`from_env` builds `storage`), `.env.example`, `tapo_storage/base.py` (`storage_from_env(environ, ctx) -> Optional[Storage]`)
- Test: `tests/test_storage_env.py`

**Interfaces:**
- Produces: env names `STORAGE` (`youtube` default | `webdav` | `local` | `rclone`), `WEBDAV_URL`, `WEBDAV_USER`, `WEBDAV_PASS`, `WEBDAV_FOLDER`, `LOCAL_ROOT`, `RCLONE_REMOTE`, `RCLONE_CONFIG`, `RCLONE_FOLDER`. `storage_from_env` returns `None` for `youtube` (so `storage_for` keeps today's path) and raises `SystemExit("Missing WEBDAV_URL in .env")`-style messages (same style as `_env`).

- [ ] **Step 1: Write failing tests**: `test_unset_storage_means_youtube_and_context_storage_is_none`, `test_storage_webdav_builds_webdav_storage`, `test_storage_local_needs_local_root`, `test_storage_rclone_needs_remote`, `test_unknown_storage_exits_with_clear_message`, `test_motion_mode_regression_with_default_env` (existing fixture `single_user_env`).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement**; document every variable in `.env.example` with a comment line each.
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `feat: choose the destination with STORAGE in single-user mode`.

### Task 7: Server settings and per-user storage table

**Files:**
- Modify: `tapo_web/config.py`, `tapo_web/db.py`
- Test: `tests/test_config.py`, `tests/test_db.py` (extend)

**Interfaces:**
- Produces `Settings` fields: `storage: str = "youtube"`, `storage_allowed: tuple = ("youtube", "webdav")`, `allow_own_storage: bool = False`, `storage_webdav_url`, `storage_webdav_user`, `storage_webdav_password`, `storage_webdav_folder`, `storage_local_root: str`, `rclone_remote`, `rclone_config`, `rclone_folder` (all `""` by default), from env `STORAGE`, `STORAGE_ALLOWED`, `ALLOW_OWN_STORAGE`, `STORAGE_WEBDAV_URL`/`_USER`/`_PASSWORD`/`_FOLDER`, `STORAGE_LOCAL_ROOT`, `STORAGE_RCLONE_REMOTE`/`_CONFIG`/`_FOLDER`. `SettingsError` for unknown kinds or when the server default is `webdav`/`local`/`rclone` and its required value is missing. `SCHEMA_VERSION = 2`; table `storage_configs(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL, config_enc BLOB, updated_at REAL NOT NULL)`; `Database.save_storage_config(user_id, kind, config_enc)`, `storage_config(user_id) -> Optional[Row]`, `delete_storage_config(user_id)`; `migrate()` upgrades a version-1 database in place and keeps its data.

- [ ] **Step 1: Write failing tests**: `test_storage_settings_defaults`, `test_storage_allowed_parsed_and_unknown_kind_rejected`, `test_webdav_default_without_url_is_rejected`, `test_storage_config_roundtrip_and_delete`, `test_deleting_user_removes_storage_config`, `test_migration_from_version_1_adds_table_and_keeps_rows` (build a v1 schema by hand with a user and a youtube link).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement**; migration is `CREATE TABLE IF NOT EXISTS` plus `UPDATE schema_version`.
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `feat: server storage settings and per-user storage table`.

### Task 8: Resolving the destination and wiring the supervisor

**Files:**
- Create: `tapo_web/storage.py`
- Modify: `tapo_web/supervisor.py`, `tapo_web/cameras.py` (`context_for`/`safe_context` accept `storage=None`)
- Test: `tests/test_storage_resolve.py`, `tests/test_supervisor.py` (extend)

**Interfaces:**
- Consumes: Tasks 1-4 backends, Task 7 settings/table, `Vault`, `assert_public_host`.
- Produces: `@dataclass(frozen=True) Resolved: kind: str; config: dict; source: "own"|"server"; notice: Optional[str]`; `resolve(settings, db, vault, user_id) -> Resolved` (own override only if `allow_own_storage` and kind in `storage_allowed` ∩ {`youtube`,`webdav`}; otherwise server default; `notice` set when a saved override had to be ignored, e.g. "Your WebDAV setting is no longer allowed: the server default is used."); `build(resolved, ctx, settings) -> Optional[Storage]` (`None` for youtube; WebDAV gets `host_check=lambda h: checked_host(h, settings.allow_private_hosts)`); `signature(resolved) -> str` included in `_signature(camera)`-style restart detection so a changed destination restarts the worker.

- [ ] **Step 1: Write failing tests**: `test_resolve_defaults_to_server_kind`, `test_resolve_uses_own_webdav_when_allowed`, `test_resolve_ignores_own_when_allow_own_storage_off`, `test_resolve_falls_back_with_notice_when_kind_removed_from_allowed`, `test_resolve_never_allows_local_or_rclone_for_users` (hand-written row with kind `local` is ignored with a notice), `test_build_webdav_rejects_private_host_unless_allowed`, `test_supervisor_gives_worker_the_resolved_storage`, `test_supervisor_gate_only_charged_for_youtube`, `test_changing_destination_restarts_worker_and_pending_clips_are_kept` (clip file still in `events/` after restart), `test_maintenance_keeps_stale_clips_for_non_youtube_users_without_link` (clips are not discarded when the destination is not YouTube), `test_user_without_youtube_link_on_youtube_still_discards_stale` (existing behaviour).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement**; `Supervisor._start` resolves per camera owner, builds the storage, passes it into the context and creates the `UploadGate` only when the kind is `youtube`.
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `feat: per-account storage resolution and supervisor wiring`.

### Task 9: "Where recordings go" page

**Files:**
- Create: `tapo_web/storage_page.py`, `tapo_web/templates/storage.html`
- Modify: `tapo_web/app.py` (register, nav link in `base.html`), `tapo_web/templates/base.html`
- Test: `tests/test_storage_page.py`

**Interfaces:**
- Produces routes: `GET /storage` (login required; shows the effective destination, its source, any notice, and, when `allow_own_storage`, a form with kind `youtube`/`webdav` limited to `storage_allowed`, and WebDAV fields `url`, `user`, `password`, `folder`), `POST /storage` (CSRF; saves encrypted config, or deletes the override when `kind=default`; validates URL scheme `https` (or `http` only with `ALLOW_PRIVATE_HOSTS`), runs `assert_public_host`), `POST /storage/test` (CSRF; builds the storage from the form values and calls `check()`; shows the `StorageError` message or "Works"). Password field is never re-rendered; leaving it empty keeps the stored one.

- [ ] **Step 1: Write failing tests**: `test_page_shows_server_default_when_no_own_allowed`, `test_form_hidden_when_allow_own_storage_off`, `test_save_webdav_encrypts_password_in_database` (raw db value lacks the plain password), `test_save_rejects_private_address`, `test_save_rejects_disallowed_kind`, `test_empty_password_keeps_old`, `test_default_choice_deletes_override`, `test_test_button_reports_success_and_error` (fake `check`), `test_post_without_csrf_is_rejected`, `test_notice_is_shown_when_override_was_ignored`, `test_page_requires_login`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** with the existing Now UI Kit templates and CSP-safe markup (no inline script).
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `feat: page to choose where recordings go`.

### Task 10: Media route with Range support and CSP

**Files:**
- Create: `tapo_web/media.py`
- Modify: `tapo_web/app.py` (CSP header, register), `tests/test_no_external_assets.py` (allow the one `frame-src`)
- Test: `tests/test_media.py`

**Interfaces:**
- Consumes: Task 8 `resolve`/`build`; `Database.camera_for_user`, `camera_by_id`; Task 1 `Storage.list_day`/`open`.
- Produces: `GET /media/<int:camera_id>/<day>/<name>` (login required; owner or admin; `day` must match `^\d{4}-\d{2}-\d{2}$`, `name` must match `^[A-Za-z0-9 ._()-]+\.mp4$` and appear in `list_day(day)`); answers `200` or `206` with `Content-Type: video/mp4`, `Accept-Ranges: bytes`, `Content-Range`, `Cache-Control: private, no-store`; unsatisfiable or malformed `Range` → `416`; unknown file → `404`; foreign camera → `404`; non-browsable storage (youtube) → `404`. A single range only; open-ended and suffix ranges supported; streams in chunks, never buffering the file.

- [ ] **Step 1: Write failing tests** with a fake storage on a local folder: `test_owner_gets_full_file_200`, `test_range_returns_206_with_content_range`, `test_suffix_and_open_ended_ranges`, `test_invalid_range_416_and_huge_range_is_clamped`, `test_other_users_camera_404`, `test_admin_can_watch_any_camera`, `test_path_traversal_names_404` (`..%2F`, `..\\`, absolute paths, `%00`), `test_name_not_in_listing_404`, `test_requires_login`, `test_youtube_destination_has_no_media_route_404`, `test_csp_allows_youtube_nocookie_frames_and_self_media_only`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement**; response via a generator wrapped in `flask.Response(..., direct_passthrough=True)`.
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `feat: stream recordings to their owner with Range support`.

### Task 11: Video cards with "Guarda qui" / "Vai al video"

**Files:**
- Modify: `tapo_web/dashboard.py` (generic `videos_section(user_id)` using the resolved storage; YouTube keeps `list_playlists` and adds `embed` ids), `tapo_web/templates/dashboard.html`, `tapo_web/static/app.js`, `tapo_web/static/app.css`
- Test: `tests/test_dashboard.py` (extend), `tests/test_media_cards.py`

**Interfaces:**
- Produces card data: `{"title", "when", "size_label", "watch": {"kind": "youtube", "video_id"} | {"kind": "file", "src": "/media/<cid>/<day>/<name>"}, "open_url": Optional[str]}`; template renders each card with a button `data-watch` ("Watch here") and, when `open_url` is set, `<a href=... target="_blank" rel="noopener noreferrer">Open the video</a>`; `app.js` creates the `<iframe src="https://www.youtube-nocookie.com/embed/<id>" allowfullscreen>` or `<video controls preload="metadata" src=...>` only on click and replaces the thumbnail with it; Italian copy "Guarda qui" / "Vai al video" lives in the template strings next to the English ones (the app's pages are English only, like the other templates: the page says "Watch here" / "Open the video"; the Italian "Guarda qui" / "Vai al video" appears in README_IT and the landing page).
- Local and rclone cards have `open_url = None`; a listing failure shows the existing "could not load videos" notice and the page still opens.

- [ ] **Step 1: Write failing tests**: `test_youtube_card_has_both_actions_and_embed_id`, `test_webdav_card_has_file_src_and_cloud_link`, `test_local_card_has_only_watch_here`, `test_iframe_not_in_initial_html` (lazy: no `<iframe>` or `<video src` before click), `test_external_link_has_noopener_noreferrer`, `test_listing_failure_still_opens_page`, `test_cards_use_only_own_camera_media_urls`, `test_video_ids_are_escaped`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** JS is a small delegated click handler in the existing `app.js`, no inline code.
- [ ] **Step 4: Run** the full suite. Expected: pass.
- [ ] **Step 5: Commit** `feat: watch here / open the video cards`.
- [ ] **Step 6: Smoke test the real server** (like the multi-account smoke): run with a local storage and a generated clip, log in, load `/`, press "Watch here" in the browser tool, and confirm the video loads and seeks (screenshot). Record what was seen.

### Task 12: Packaging and documentation

**Files:**
- Modify: `Dockerfile` (copy `tapo_storage`, `apt-get install rclone`), `README.md`, `README_IT.md`, `.env.example`, `index.html` (section "Choose where recordings go", FAQ entry), `assets/site.css` if needed, `requirements.txt` (unchanged, assert it)
- Test: `tests/test_packaging.py`

**Interfaces:** none new.

- [ ] **Step 1: Write failing tests**: `test_dockerfile_copies_storage_package_and_installs_rclone`, `test_requirements_txt_still_three_packages`, `test_env_example_documents_every_storage_variable` (each env name read by `Settings`/`storage_from_env` appears in `.env.example`), `test_index_mentions_storage_choices_and_stays_asset_free` (no external URLs besides the existing ones).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Write the docs**: README EN/IT section "Where do the recordings go?" (table of the four kinds, who may choose, Mega through rclone with the note that Mega support is not tested by the author, audio note, `/media` privacy note), landing-page section and FAQ, `privacy.html` unchanged unless it says recordings only go to YouTube (then add one sentence and note it in the final message).
- [ ] **Step 4: Run** the full suite. Expected: pass; then scan the tree for the author's private strings. Expected: no match.
- [ ] **Step 5: Commit** `docs: storage destinations in README, landing page and Docker image`.

### Final steps (after Task 12)

- [ ] Review the whole branch (package via `review-package`), fix Critical/Important findings test-first, ledger the minors.
- [ ] Finish with `superpowers:finishing-a-development-branch` (merge/push only on the user's say-so).
- [ ] Manual checks left to the user: a real Nextcloud WebDAV upload, a real Mega remote through rclone, playing a real Tapo clip (audio codec) in the browser.
