# Multi-account mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a trusted group share one tapo-uploader server: each user links their own camera and their own YouTube channel, and sees only their own videos and camera status in a web page.

**Architecture:** The recording, ONVIF and upload code stops reading module-level environment and takes a `CameraContext` object instead; single-user mode builds that context from `.env` and behaves as today. A new package `tapo_web` adds a Flask app (server-rendered pages, SQLite, encrypted secrets), a supervisor that runs one worker per enabled camera, and the login, YouTube linking, quota and onboarding pieces.

**Tech Stack:** Python 3.10+, Flask, Authlib, cryptography (Fernet), SQLite (`sqlite3`), waitress, pytest, Now UI Kit (Bootstrap 4, jQuery, MIT) vendored as static files.

**Spec:** `docs/superpowers/specs/2026-10-26-multi-account-design.md` (the plan argues from it; read it first).

## Global Constraints

- Without `MULTIUSER=1` the programs `tapo_uploader.py` and `tapo_motion.py` behave exactly as before (same `.env`, same commands, same files).
- Python 3.10 or newer; no dependency or file loaded from the internet at runtime (no CDN, no web fonts).
- Camera passwords and YouTube tokens are stored encrypted (Fernet); the service refuses to start in multi-account mode without `ENCRYPTION_KEY`; secrets never appear in logs or pages.
- Roles: Nextcloud group `NEXTCLOUD_ADMIN_GROUP` (default `admin`) is admin; `NEXTCLOUD_USER_GROUP` allows normal users; if it is unset only admins can log in.
- Quota: default `QUOTA_DAILY=10000`; upload cost 1600, playlist insert 50, playlist item insert 50, any list call 1; the day resets at midnight Pacific Time.
- Defaults: one camera per user (`MAX_CAMERAS_PER_USER=1`), `MAX_CAMERAS_TOTAL=20`, data kept 30 days after a user is deactivated (`RETENTION_DAYS=30`), clips of an unlinked YouTube account discarded after 7 days.
- Private, loopback, link-local and cloud-metadata camera addresses are refused unless `ALLOW_PRIVATE_HOSTS=1`.
- Never commit `.env`, `client_secret.json`, `token.json`, `state.json`, recordings; commit author is the GitHub noreply address; work in a clone of the repository, not in the folder that holds the author's secrets.
- Now UI Kit's `LICENSE.md` (MIT, Creative Tim) is copied next to the vendored files. The Nucleo icon font is NOT copied: its license (`assets/fonts/nucleo-license.md` in the kit) forbids redistribution, so pages use own inline SVG icons and the `@font-face` for "Nucleo Outline" is removed from the copied CSS.

## Review Focus

Failure modes the spec implies; each has a test in the task named in brackets.

1. A camera host that resolves to a public address at the first check and to a private one at connection time (DNS rebinding). [Task 6]
2. A logged-in user requesting another user's camera or YouTube data by changing an id in the URL. [Tasks 5, 10, 13]
3. A Nextcloud user with an empty group list, or group names that differ only by case. [Task 8]
4. A YouTube token revoked or expired in the middle of the day: the worker must pause and report, not crash or lose clips. [Task 12]
5. A camera password with `!`, `@`, `:`, `%` or a space: the RTSP URL and the ONVIF digest must still work and the password must never reach a log. [Tasks 2, 3]

---

### Task 1: Test harness and characterization of the current behavior

**Files:**
- Create: `tests/conftest.py`, `tests/test_characterization.py`, `requirements-dev.txt`
- Modify: none

**Interfaces:**
- Produces: a `single_user_env` pytest fixture that sets `CAMERA_USER=cam`, `CAMERA_PASS=p@ss:w!rd`, `CAMERA_HOST=192.168.1.37`, `CAMERA_PORT=554`, `CAMERA_STREAM=stream1`, `DATA_DIR=<tmp_path>` and reloads `tapo_uploader` and `tapo_motion`.

- [ ] **Step 1: Write the tests against the current code** in `tests/test_characterization.py`:
  - `test_rtsp_url_quotes_credentials`: `rtsp_url() == "rtsp://cam:p%40ss%3Aw%21rd@192.168.1.37:554/stream1"`
  - `test_scrub_hides_password`: `scrub(rtsp_url()) == "rtsp://***@192.168.1.37:554/stream1"`
  - `test_ffmpeg_cmd_segments`: command starts with `["ffmpeg", "-hide_banner", "-loglevel", "error"]`, contains `-c:v copy`, `-segment_atclocktime 1`, `movflags=+frag_keyframe+empty_moov+default_base_moof`, and ends with `str(out_dir / "%Y%m%d_%H%M%S.mp4")`.
  - `test_motion_states_value_before_name`: a reply with `SimpleItem Value="true" Name="IsMotion"` yields `[("IsMotion", True)]`; `Name="IsPeople"` is accepted; `Name="Other"` is ignored.
  - `test_on_motion_extends_window`: `on_motion(None, 100.0)` gives `Event(100.0, 400.0)`; `on_motion(Event(100.0, 400.0), 380.0).end == 680.0`; `on_motion(Event(100.0, 400.0), 200.0).end == 400.0`.
  - `test_timestamp`: `timestamp(65) == "1:05"`, `timestamp(3725) == "1:02:05"`.
  - `test_segments_sorted`: files `20261026_100000.mp4` and `20261026_095950.mp4` in `BUFFER_DIR` come back oldest first.
  - `test_due_days`: a day folder dated yesterday is due; today's is due only after `UPLOAD_AT`, or with `force=True`.
- [ ] **Step 2: Run** `pytest tests/test_characterization.py -v`. Expected: all PASS (they describe today's behavior; if one fails, fix the test, not the code).
- [ ] **Step 3: Add `requirements-dev.txt`** with `pytest>=8`; the fixture clears `sys.modules` entries for the two modules so each test sees its own environment.
- [ ] **Step 4: Commit** `git add tests requirements-dev.txt && git commit -m "test: characterize single-user behavior"`

### Task 2: `CameraContext` and the uploader module

**Files:**
- Create: `camera_context.py`, `tests/test_camera_context.py`
- Modify: `tapo_uploader.py` (all functions that read camera or path settings)

**Interfaces:**
- Produces: `CameraContext` (frozen dataclass) with fields `name: str, host: str, port: int = 554, user: str = "", password: str = "", stream: str = "stream1", onvif_host: str = "", onvif_port: int = 2020, data_dir: Path = Path("."), pre: int = 30, post: int = 300, extend_within: int = 30, segment: int = 10, segment_seconds: int = 21600, upload_at: datetime.time = time(21, 0), motion_events: frozenset[str] = frozenset({"ismotion", "ispeople"}), playlist_prefix: str = "Tapo", privacy: str = "private", youtube: Callable[[], Resource] | None = None`; methods `rtsp_url() -> str`, `scrub(text: str) -> str`; properties `record_dir`, `buffer_dir`, `events_dir`, `state_file` (all under `data_dir`); classmethod `from_env() -> CameraContext` reading exactly the variables the modules read today.
- `tapo_uploader` functions take the context as first parameter: `ffmpeg_cmd(ctx, out_dir, segment_seconds)`, `record_loop(ctx, out_dir, segment_seconds, stop)`, `load_state(ctx)`, `save_state(ctx, state)`, `playlist_for(ctx, yt, state, day)`, `upload(ctx, yt, state, path, start, title=None, description="")`, `upload_loop(ctx, out_dir, stop)`, `youtube_client(ctx)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_camera_context.py`: `test_from_env_matches_module_values` (every field equals what Task 1's fixture sets); `test_rtsp_url_special_characters` for passwords `a b`, `x:y@z`, `100%`, `!`; `test_scrub_hides_password_everywhere` (the quoted credentials never survive `scrub`); `test_state_file_per_data_dir` (two contexts with different `data_dir` write different `state.json`).
- [ ] **Step 2: Run** `pytest tests/test_camera_context.py -v`. Expected: FAIL, `camera_context` does not exist.
- [ ] **Step 3: Implement `CameraContext` in `camera_context.py`**, moving `camera_credentials`, `rtsp_url` and `scrub` logic into its methods.
- [ ] **Step 4: Refactor `tapo_uploader.py`** so every function above takes `ctx`; the module-level `DATA`, `STATE_FILE`, `env()` reads stay only inside `from_env` and `main`; `cmd_check`, `cmd_test`, `cmd_run` build `ctx = CameraContext.from_env()` and pass it down; `youtube_client(ctx)` uses `ctx.youtube` when set, otherwise the existing file/env flow.
- [ ] **Step 5: Update Task 1's tests** to call the new signatures (assertions unchanged); run `pytest -v`. Expected: all PASS.
- [ ] **Step 6: Commit** `git commit -am "refactor: pass a CameraContext through the uploader"`

### Task 3: Motion module on the context and a reusable `CameraWorker`

**Files:**
- Create: `camera_worker.py`, `tests/test_camera_worker.py`
- Modify: `tapo_motion.py`

**Interfaces:**
- Consumes: `CameraContext` from Task 2.
- Produces: in `tapo_motion`, the same functions with `ctx` first: `soap_envelope(ctx, body)`, `soap(ctx, url, body, timeout=30)`, `reach(ctx, url)`, `subscribe(ctx)`, `motion_states(ctx, reply)`, `onvif_loop(ctx, events, stop)`, `segments(ctx)`, `prune_buffer(ctx, event, now)`, `save_clip(ctx, event)`, `on_motion(ctx, event, at)`, `controller(ctx, events, stop)`, `due_days(ctx, force=False)`, `upload_day(ctx, yt, state, day_dir)`, `upload_pending(ctx, state, force=False)`, `daily_loop(ctx, stop)`. `CameraWorker(ctx, on_status: Callable[[str, str], None])` with `start()`, `stop()`, `alive: bool`; `on_status(kind, detail)` is called with kinds `"online"`, `"offline"`, `"motion"`, `"uploaded"`, `"error"`.

- [ ] **Step 1: Write the failing tests**: `test_soap_digest_uses_ctx_password` (the digest in `soap_envelope(ctx, "")` verifies against `ctx.password` and the XML never contains the password); `test_two_contexts_do_not_share_buffer` (segments of ctx A are not seen by ctx B); `test_motion_states_respects_ctx_events` (with `motion_events=frozenset({"ispeople"})` an `IsMotion` item is ignored); `test_worker_reports_error_without_crashing` (a worker whose `youtube` factory raises reports `("error", ...)` and `alive` stays true).
- [ ] **Step 2: Run** `pytest tests/test_camera_worker.py -v`. Expected: FAIL.
- [ ] **Step 3: Refactor `tapo_motion.py`**: remove the module-level constants (`PRE`, `POST`, `BUFFER_DIR`, `ONVIF_HOST`...) in favor of `ctx` fields; `cmd_run`/`cmd_listen`/`cmd_upload` build the context from the environment.
- [ ] **Step 4: Implement `CameraWorker` in `camera_worker.py`**: starts the four threads of `cmd_run` (record, onvif, controller, daily upload) for one context, with a shared `stop` event; wraps them so an exception is passed to `on_status("error", scrub(str(err)))`; `stop()` joins within 10 s.
- [ ] **Step 5: Run** `pytest -v`. Expected: all PASS, including Task 1.
- [ ] **Step 6: Manual check** (single-user unchanged): `python tapo_motion.py listen 10` against the real camera still prints its events.
- [ ] **Step 7: Commit** `git commit -am "refactor: motion module on CameraContext, add CameraWorker"`

### Task 4: Settings and the vault

**Files:**
- Create: `tapo_web/__init__.py`, `tapo_web/config.py`, `tapo_web/crypto.py`, `tests/test_config.py`, `tests/test_crypto.py`
- Modify: `requirements.txt` (add `Flask>=3.0`, `Authlib>=1.3`, `cryptography>=42`, `waitress>=3.0`, `requests>=2.31`)

**Interfaces:**
- Produces: `Settings` (frozen dataclass) with `multiuser: bool, public_url: str, secret_key: str, encryption_key: str, server_public_ip: str, port_range: tuple[int, int], data_dir: Path, nextcloud_url: str, nextcloud_client_id: str, nextcloud_client_secret: str, nextcloud_admin_group: str, nextcloud_user_group: str | None, google_client_json: str | None, allow_own_google_client: bool, quota_daily: int, upload_at: time, upload_stagger_minutes: int, max_cameras_per_user: int, max_cameras_total: int, retention_days: int, allow_private_hosts: bool, local_login: bool`; `Settings.from_env(environ: Mapping[str, str]) -> Settings`; `SettingsError(ValueError)`. `Vault(key: str)` with `encrypt(text: str) -> str`, `decrypt(token: str) -> str`, `VaultError`.

- [ ] **Step 1: Write the failing tests**: `test_defaults` (`quota_daily == 10000`, `max_cameras_per_user == 1`, `retention_days == 30`, `nextcloud_admin_group == "admin"`, `nextcloud_user_group is None`, `allow_private_hosts is False`); `test_multiuser_requires_keys` (`MULTIUSER=1` without `ENCRYPTION_KEY` or `SECRET_KEY` raises `SettingsError`); `test_single_user_needs_no_keys`; `test_vault_roundtrip` and `test_vault_wrong_key_raises`; `test_vault_output_not_plaintext`.
- [ ] **Step 2: Run** `pytest tests/test_config.py tests/test_crypto.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `Settings.from_env` (names exactly as in the Global Constraints and spec section 6/12) and `Vault` on `cryptography.fernet.Fernet`.
- [ ] **Step 4: Run** the same tests. Expected: PASS.
- [ ] **Step 5: Commit** `git add tapo_web requirements.txt tests && git commit -m "feat: settings and vault for multi-account mode"`

### Task 5: Database and per-user access

**Files:**
- Create: `tapo_web/db.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `Vault` (Task 4).
- Produces: `Database(path: Path | str)` with `migrate()`; users: `add_user(origin: str, username: str, display_name: str, role: str, password_hash: str | None = None) -> int`, `user_by_username(origin, username) -> dict | None`, `user_by_id(user_id) -> dict | None`, `set_user_active(user_id: int, active: bool)`, `set_user_role(user_id: int, role: str)`, `list_users() -> list[dict]`; cameras: `add_camera(user_id: int, name: str, host: str, port: int, onvif_port: int, stream: str, username_enc: str, password_enc: str) -> int`, `camera_for_user(user_id: int, camera_id: int) -> dict | None` (returns `None` for another user's camera), `cameras_for_user(user_id) -> list[dict]`, `all_enabled_cameras() -> list[dict]`, `set_camera_status(camera_id: int, **fields)`, `delete_camera(user_id, camera_id)`; YouTube: `save_youtube_link(user_id, token_enc, channel_title, client_source, client_secret_enc=None)`, `youtube_link(user_id) -> dict | None`, `delete_youtube_link(user_id)`; quota/events: `add_quota_use(day: str, units: int)`, `quota_used(day: str) -> int`, `add_event(camera_id, kind, detail)`, `purge_events(older_than_days: int)`. Tables per spec section 6.

- [ ] **Step 1: Write the failing tests**: `test_user_roundtrip`; `test_camera_for_user_hides_other_users_camera` (user B gets `None` for A's camera id); `test_cascade_delete_user_data`; `test_quota_accumulates_per_day`; `test_migrate_is_idempotent`; `test_stored_camera_password_is_ciphertext` (the raw column differs from the plaintext).
- [ ] **Step 2: Run** `pytest tests/test_db.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `Database`** with `sqlite3`, `PRAGMA foreign_keys=ON`, a schema version table, and parameterised queries only.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: SQLite database with per-user camera access"`

### Task 6: Camera address safety (SSRF)

**Files:**
- Create: `tapo_web/security.py`, `tests/test_security.py`

**Interfaces:**
- Produces: `UnsafeHost(ValueError)`; `assert_public_host(host: str, allow_private: bool = False, resolver=socket.getaddrinfo) -> list[str]` returns the resolved IPs, raises `UnsafeHost` if any is private, loopback, link-local, unspecified, multicast, or in `169.254.169.254`, `100.64.0.0/10`, `fc00::/7`; `checked_host(host, allow_private=False, resolver=...) -> str` returns one validated IP to connect to.

- [ ] **Step 1: Write the failing tests**: `test_rejects_private_and_loopback` for `192.168.1.5`, `10.0.0.1`, `127.0.0.1`, `::1`, `169.254.169.254`, `100.64.0.1`, `0.0.0.0`; `test_accepts_public` (`93.184.216.34`); `test_rejects_name_resolving_privately` (fake resolver returns `10.0.0.5` for `cam.example.com`); `test_rebinding_second_lookup_is_checked` (resolver returns `93.184.216.34` first, `127.0.0.1` second: calling `checked_host` twice raises on the second call); `test_allow_private_flag`; `test_rejects_mixed_results` (one public and one private address); `test_rejects_non_hostname_inputs` (`"a b"`, `"http://x"`, `""`, `"cam:554"`).
- [ ] **Step 2: Run** `pytest tests/test_security.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** both functions with `ipaddress`; validate the string as a hostname or IP literal before resolving.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: refuse unsafe camera addresses"`

### Task 7: Flask app, local login, vendored UI, first admin

**Files:**
- Create: `tapo_web/app.py`, `tapo_web/auth_local.py`, `tapo_web/__main__.py`, `tapo_web/templates/base.html`, `tapo_web/templates/login.html`, `tapo_web/static/vendor/now-ui-kit/…` (CSS, JS, fonts, icons, `LICENSE.md` copied from `github.com/creativetimofficial/now-ui-kit`), `tests/test_app_login.py`, `tests/test_no_external_assets.py`
- Modify: `Dockerfile` (copy `tapo_web`)

**Interfaces:**
- Consumes: `Settings`, `Database`, `Vault`.
- Produces: `create_app(settings: Settings, db: Database | None = None) -> Flask`; decorators `login_required`, `admin_required`; `current_user() -> dict | None`; session cookie with `HttpOnly`, `Secure` (when `public_url` is https), `SameSite=Lax`; CSRF token check on every POST (`csrf_token()` available in templates); `hash_password(plain: str) -> str` and `verify_password(plain: str, hashed: str) -> bool` (scrypt); CLI `python -m tapo_web create-admin` and `python -m tapo_web run`.

- [ ] **Step 1: Write the failing tests**: `test_login_ok_and_logout`; `test_wrong_password_rejected_and_rate_limited` (after 5 failures in a minute the 6th returns 429 even with the right password); `test_post_without_csrf_rejected`; `test_disabled_user_cannot_log_in`; `test_anonymous_redirected_to_login`; `test_cookie_flags`; `test_create_admin_command_creates_admin_once`; `test_no_external_assets` renders `/login` and asserts every `src=` and `href=` in the HTML and in the vendored CSS `url()` values is relative or starts with `/`.
- [ ] **Step 2: Run** `pytest tests/test_app_login.py tests/test_no_external_assets.py -v`. Expected: FAIL.
- [ ] **Step 3: Copy the vendored assets** from Now UI Kit: Bootstrap 4 CSS/JS, jQuery, Popper and the kit's CSS, and its `LICENSE.md`; do not copy the Nucleo font (remove its `@font-face`), the kit's stock photos or any Google Fonts link, and use the system font stack and inline SVG icons; layout in `base.html` with a light/dark toggle stored in `localStorage`.
- [ ] **Step 4: Implement** the app factory, local login, CSRF, rate limit (in-memory per IP and username), and the two CLI commands (`create-admin` also reads `ADMIN_USERNAME`/`ADMIN_PASSWORD` at first start).
- [ ] **Step 5: Run** the tests. Expected: PASS. Then open `/login` in a browser and check both themes by eye.
- [ ] **Step 6: Commit** `git add tapo_web tests Dockerfile && git commit -m "feat: web app skeleton with local login and Now UI Kit"`

### Task 8: Nextcloud login and roles

**Files:**
- Create: `tapo_web/auth_nextcloud.py`, `tests/test_nextcloud.py`
- Modify: `tapo_web/app.py` (routes `/auth/nextcloud/start`, `/auth/nextcloud/callback`; login page shows the button only if configured)

**Interfaces:**
- Consumes: `Settings`, `Database`.
- Produces: `role_from_groups(groups: list[str], admin_group: str, user_group: str | None) -> str | None` returns `"admin"`, `"user"` or `None` (exact, case-sensitive group names; admin wins; `None` means no access); `fetch_nextcloud_user(settings, access_token: str, http=requests) -> dict` returning `{"id": str, "display_name": str, "groups": list[str]}` from `GET {nextcloud_url}/ocs/v2.php/cloud/user?format=json` with a Bearer token.

- [ ] **Step 1: Write the failing tests**: `test_admin_group_gives_admin`; `test_user_group_gives_user`; `test_admin_wins_over_user`; `test_no_user_group_configured_only_admins_enter` (a user in group `friends` gets `None`); `test_group_names_are_case_sensitive` (`Admin` ≠ `admin`); `test_empty_groups_denied`; `test_callback_creates_user_with_role` (fake Nextcloud via a stubbed `http`); `test_role_re_evaluated_each_login` (a user removed from the group is refused and marked inactive on the next login); `test_state_mismatch_rejected`; `test_nextcloud_error_shows_message_not_traceback`.
- [ ] **Step 2: Run** `pytest tests/test_nextcloud.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with Authlib's OAuth2 client (authorize `…/index.php/apps/oauth2/authorize`, token `…/index.php/apps/oauth2/api/v1/token`); upsert the user by `("nextcloud", id)`; store no Nextcloud token after the login.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: Nextcloud login with roles from groups"`

### Task 9: Linking a YouTube channel

**Files:**
- Create: `tapo_web/youtube_link.py`, `tests/test_youtube_link.py`
- Modify: `tapo_web/app.py` (routes `/youtube/connect`, `/youtube/callback`, `/youtube/disconnect`)

**Interfaces:**
- Consumes: `Settings`, `Database`, `Vault`.
- Produces: `build_flow(settings, redirect_uri: str, client_json: str | None = None) -> Flow`; `client_json_for(settings, vault, link_or_user) -> str` (own client if the user has one and `allow_own_google_client` is true, else the shared one); `credentials_for(vault, link: dict) -> google.oauth2.credentials.Credentials`; `youtube_for_user(db, vault, settings, user_id) -> Resource | None` (`None` if not linked); `LinkRevoked(Exception)`.

- [ ] **Step 1: Write the failing tests**: `test_connect_redirects_with_state_and_pkce` (the authorization URL has `state` and `code_challenge`); `test_callback_rejects_wrong_state`; `test_callback_stores_encrypted_token_and_channel_title` (stubbed flow); `test_disconnect_deletes_token`; `test_own_client_used_only_when_allowed`; `test_user_cannot_read_other_users_link` (route and helper); `test_refresh_failure_raises_link_revoked`.
- [ ] **Step 2: Run** `pytest tests/test_youtube_link.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with `google_auth_oauthlib.flow.Flow.from_client_config`, scope `https://www.googleapis.com/auth/youtube`, `access_type="offline"`, `prompt="consent"`; the channel title comes from `channels().list(part="snippet", mine=True)`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: link a YouTube channel per user"`

### Task 10: Cameras, ports, router guide and connection test

**Files:**
- Create: `tapo_web/cameras.py`, `tapo_web/guide.py`, `tapo_web/templates/camera_*.html`, `tests/test_cameras.py`, `tests/test_guide.py`
- Modify: `tapo_web/app.py` (routes `/cameras/new`, `/cameras/<id>`, `/cameras/<id>/guide`, `/cameras/<id>/test`)

**Interfaces:**
- Consumes: `Database`, `Vault`, `security.checked_host`, `CameraContext`.
- Produces: `assign_ports(taken: set[int], start: int, end: int, rng: random.Random | None = None) -> tuple[int, int]` (two unused ports in the range, never adjacent); `Guide` dataclass `(manual_steps: list[str], ai_prompt: str)` and `build_guide(settings, host: str, rtsp_port: int, onvif_port: int) -> Guide`; `context_for(camera: dict, vault: Vault, settings: Settings, youtube=None) -> CameraContext`; `ProbeResult(tcp: bool, rtsp: bool, onvif: bool, detail: str)` and `probe_camera(ctx, runner=subprocess.run) -> ProbeResult`.

- [ ] **Step 1: Write the failing tests**: `test_assign_ports_unique_in_range_not_adjacent`; `test_assign_ports_exhausted_raises`; `test_ai_prompt_has_server_ip_and_ports_but_no_secrets` (contains `settings.server_public_ip`, both external ports, `554` and `2020`; contains neither the camera user nor its password); `test_manual_steps_say_allow_only_server_ip`; `test_camera_limit_per_user`; `test_total_camera_limit`; `test_user_cannot_open_other_users_camera_pages` (404 on view, edit, guide, test, delete); `test_unsafe_host_refused_with_message`; `test_password_never_rendered` (not in any camera page HTML, also on the edit form); `test_probe_reports_each_stage` (fake runner: TCP open, ffprobe fails → `rtsp is False`).
- [ ] **Step 2: Run** `pytest tests/test_cameras.py tests/test_guide.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the pages and helpers; the camera form asks name, DDNS host, camera account user and password; ports are shown read-only after assignment; the camera stays disabled until a probe succeeds.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: cameras, port assignment, router guide, connection test"`

### Task 11: Supervisor, status and retention

**Files:**
- Create: `tapo_web/supervisor.py`, `tests/test_supervisor.py`
- Modify: `tapo_web/__main__.py` (`run` starts the supervisor and the web server together)

**Interfaces:**
- Consumes: `CameraWorker` (Task 3), `Database`, `context_for`.
- Produces: `Supervisor(db, vault, settings, worker_factory=CameraWorker, clock=time.time)` with `sync() -> None` (start workers for enabled cameras of active users, stop the others; check host safety before each start), `status(camera_id: int) -> dict`, `stop_all()`; `purge_inactive(db, data_dir: Path, now: datetime, retention_days: int) -> int` (returns users purged); `discard_stale_clips(events_dir: Path, now: datetime, max_age_days: int = 7) -> int`.

- [ ] **Step 1: Write the failing tests** with a fake `worker_factory`: `test_sync_starts_only_enabled_cameras_of_active_users`; `test_deactivated_user_worker_stopped_on_sync`; `test_unsafe_host_not_started_and_error_recorded`; `test_status_events_update_camera_row`; `test_purge_after_retention_removes_data_and_tokens` (30 days: kept at day 29, purged at day 31); `test_stale_clips_discarded_after_seven_days_without_link`; `test_worker_crash_restarts_with_backoff`.
- [ ] **Step 2: Run** `pytest tests/test_supervisor.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the supervisor with one daemon thread calling `sync()` every 30 s and the retention jobs hourly.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: supervisor, camera status and data retention"`

### Task 12: Quota ledger and staggered uploads

**Files:**
- Create: `tapo_web/quota.py`, `tests/test_quota.py`
- Modify: `tapo_motion.py` (`daily_loop` and `upload_pending` accept an optional `gate` object), `tapo_web/supervisor.py`

**Interfaces:**
- Consumes: `Database.add_quota_use`, `Database.quota_used`, `youtube_for_user`, `LinkRevoked`.
- Produces: constants `UPLOAD_COST = 1600`, `PLAYLIST_ITEM_COST = 50`, `PLAYLIST_INSERT_COST = 50`, `LIST_COST = 1`; `pacific_day(now: datetime) -> str` (`YYYY-MM-DD` in `America/Los_Angeles`); `QuotaLedger(db, daily_limit: int)` with `record(units: int, now: datetime | None = None)`, `used(now=None) -> int`, `can_afford(units: int, now=None) -> bool`; `stagger_slots(camera_ids: list[int], upload_at: time, stagger_minutes: int) -> dict[int, time]` (sorted ids, `upload_at + n * stagger_minutes`, wrapping within the day); `UploadGate(ledger, camera_id, slot: time)` with `allowed(now: datetime) -> bool`.

- [ ] **Step 1: Write the failing tests**: `test_pacific_day_boundary` (07:30 UTC on 2026-10-27 is still 2026-10-26 in Pacific during DST; 08:30 UTC is 2026-10-27); `test_dst_change_day` (2026-11-01 handled); `test_can_afford_stops_at_limit` (limit 10000, five uploads of 1650 recorded → a sixth is refused, with the 50-unit extras counted); `test_stagger_slots` (`[7, 3, 9]`, `21:00`, `10` → `{3: 21:00, 7: 21:10, 9: 21:20}`); `test_gate_waits_for_slot_and_quota`; `test_revoked_token_pauses_uploads_and_keeps_clips` (a `LinkRevoked` leaves the clip files in place, sets camera status `error`, and the next day-loop tick retries without raising); `test_quota_error_keeps_clips`.
- [ ] **Step 2: Run** `pytest tests/test_quota.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the ledger and gate; the supervisor builds each camera's context with `youtube=lambda: youtube_for_user(...)` and passes the gate; every upload path records its units after success (and the list calls it makes).
- [ ] **Step 4: Run** `pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: quota ledger and staggered uploads"`

### Task 13: Personal page (videos and status)

**Files:**
- Create: `tapo_web/dashboard.py`, `tapo_web/templates/dashboard.html`, `tests/test_dashboard.py`
- Modify: `tapo_web/app.py` (route `/`)

**Interfaces:**
- Consumes: `youtube_for_user`, `Supervisor.status`, `Database`.
- Produces: `list_playlists(yt, prefix: str, limit: int = 10) -> list[dict]` returning `{"id", "title", "url", "videos": [{"id", "title", "thumbnail", "published", "url"}]}` for playlists whose title starts with the prefix, newest first; `camera_cards(db, supervisor, user_id) -> list[dict]` with `online`, `last_motion`, `last_upload`, `pending_clips`, `last_error`, `name`.

- [ ] **Step 1: Write the failing tests**: `test_list_playlists_filters_by_prefix_and_sorts` (fake `yt` objects); `test_video_urls_point_to_youtube` (`https://www.youtube.com/watch?v=<id>`); `test_dashboard_shows_only_own_cameras_and_playlists`; `test_dashboard_without_youtube_link_shows_connect_button`; `test_youtube_error_shows_message_not_traceback`; `test_dashboard_has_no_edit_or_delete_video_controls`.
- [ ] **Step 2: Run** `pytest tests/test_dashboard.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** reading through the user's own client (one `playlists().list` page plus one `playlistItems().list` per playlist, cached 5 minutes per user) and the cards, in the kit's card style.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: personal page with videos and camera status"`

### Task 14: Admin page

**Files:**
- Create: `tapo_web/admin.py`, `tapo_web/templates/admin.html`, `tests/test_admin.py`
- Modify: `tapo_web/app.py`

**Interfaces:**
- Consumes: `Database`, `Supervisor`, `QuotaLedger`, `admin_required`.
- Produces: routes `GET /admin`, `POST /admin/users/<id>/active`, `POST /admin/users/<id>/role`; the page lists users (origin, role, active, cameras), all cameras with status, the estimated quota used today against the limit, and the main settings read-only (secrets masked).

- [ ] **Step 1: Write the failing tests**: `test_non_admin_gets_403_on_admin_routes`; `test_admin_can_deactivate_user_and_workers_stop` (calls `supervisor.sync`); `test_admin_cannot_remove_last_admin`; `test_quota_shown`; `test_settings_masked` (no key, secret or password value in the HTML).
- [ ] **Step 2: Run** `pytest tests/test_admin.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the page and actions with CSRF on the POSTs.
- [ ] **Step 4: Run** `pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: admin page"`

### Task 15: Packaging, docs and the single-user regression

**Files:**
- Modify: `Dockerfile`, `deploy/` (new `tapo-web.service`), `README.md`, `README_IT.md`, `.env.example`, `render.yaml` (comment only)
- Create: `tests/test_single_user_regression.py`

**Interfaces:** none new.

- [ ] **Step 1: Write the regression test**: with `MULTIUSER` unset, `python tapo_uploader.py` and `python tapo_motion.py` import without Flask installed (simulate with `sys.modules` blocking `flask`) and expose the same commands (`auth`, `check`, `test`, `run`; `listen`, `run`, `upload`).
- [ ] **Step 2: Run** `pytest tests/test_single_user_regression.py -v`. Expected: PASS.
- [ ] **Step 3: Write the docs** (English `README.md` section "Multi-account mode", Italian in `README_IT.md`): what it is, the quota warning (about 5 to 6 users at one upload a day on a shared Google project), Nextcloud setup (create the OAuth 2.0 client under Security, set `NEXTCLOUD_*`, groups), `PUBLIC_URL` and HTTPS reverse proxy, generating `ENCRYPTION_KEY`/`SECRET_KEY`, first admin, onboarding a friend, the SSRF option, the data-retention rules; `.env.example` lists every new variable; `deploy/tapo-web.service` runs `python -m tapo_web run`; the Dockerfile installs the new requirements.
- [ ] **Step 4: Run the whole suite**: `pytest -v`. Expected: all PASS.
- [ ] **Step 5: Manual end-to-end** on a test server: log in through Nextcloud as admin, add a camera, run the connection test, link YouTube, trigger motion, run `UPLOAD_AT` in the past, and see the video on the personal page.
- [ ] **Step 6: Commit and push** `git commit -am "docs: multi-account mode, packaging" && git push`

---

## Self-review notes

- **Spec coverage:** goals and constraints (Tasks 2-4); data model (5); SSRF (6); login, local and Nextcloud (7, 8); YouTube linking and own client (9); onboarding guide, ports, connection test (10); supervisor, retention, status (11); quota and stagger (12); personal and admin pages (13, 14); compatibility, docs, packaging (15); UI kit and licence (7).
- **Types:** `CameraContext` (Task 2) is the type consumed in Tasks 3, 10, 11, 12; `youtube_for_user` (Task 9) is the one used in Tasks 11-13; `QuotaLedger`/`UploadGate` (Task 12) names are used only there and in the supervisor.
- **Proportion:** the plan is longer than the spec because the work is a refactor plus a new service; code appears only as test assertions and signatures.
