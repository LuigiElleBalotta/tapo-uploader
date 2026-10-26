"""The page where a user sees and changes where their recordings go."""
import json

import pytest

from tapo_storage.base import StorageError
from tests.conftest import Env

GOOD = {"kind": "webdav", "url": "https://cloud.friend.example/remote.php/dav/files/me", "user": "me",
        "password": "S3cretPW", "folder": "Tapo"}


def setup(tmp_path, **settings):
    env = Env(tmp_path, **settings)
    env.add_local_user("alice", "correct horse")
    client = env.app.test_client()
    env.login("alice", "correct horse", client=client)
    return env, client


def post(env, client, path="/storage", **fields):
    return client.post(path, data={"csrf_token": env.csrf(client, "/storage"), **fields})


def saved(env):
    uid = env.db.user_by_username("local", "alice")["id"]
    return env.db.storage_config(uid), uid


def test_page_requires_login(web):
    assert web.client.get("/storage").status_code == 302
    assert web.client.post("/storage").status_code in (302, 400)


def test_page_shows_server_default_when_no_own_allowed(tmp_path):
    env, client = setup(tmp_path)
    page = client.get("/storage").get_data(as_text=True)
    assert "YouTube" in page and "server" in page.lower()
    assert 'name="kind"' not in page          # no form: the server does not allow own destinations
    assert 'name="password"' not in page


def test_form_hidden_when_allow_own_storage_off_and_save_is_forbidden(tmp_path):
    env, client = setup(tmp_path)
    assert post(env, client, **GOOD).status_code == 403
    assert saved(env)[0] is None


def test_form_offers_only_allowed_kinds(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True, storage_allowed=("webdav",))
    page = client.get("/storage").get_data(as_text=True)
    assert 'value="webdav"' in page and 'value="default"' in page and 'value="youtube"' not in page


def test_save_webdav_encrypts_password_in_database(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    response = post(env, client, **GOOD)
    assert response.status_code == 302
    row, uid = saved(env)
    assert row["kind"] == "webdav" and "S3cretPW" not in row["config_enc"] and "cloud.friend.example" not in row["config_enc"]
    config = json.loads(env.vault.decrypt(row["config_enc"]))
    assert config == {"url": GOOD["url"], "user": "me", "password": "S3cretPW", "folder": "Tapo"}


def test_password_is_never_rendered_back(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    post(env, client, **GOOD)
    page = client.get("/storage").get_data(as_text=True)
    assert "S3cretPW" not in page and "cloud.friend.example" in page and 'value="me"' in page


def test_save_rejects_private_address(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    response = post(env, client, **{**GOOD, "url": "https://10.0.0.5/dav"})
    assert response.status_code == 400 and "non-public" in response.get_data(as_text=True)
    assert saved(env)[0] is None


def test_private_address_allowed_when_the_admin_allows_it(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True, allow_private_hosts=True)
    assert post(env, client, **{**GOOD, "url": "https://10.0.0.5/dav"}).status_code == 302


def test_save_rejects_plain_http_and_bad_urls(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    for url in ("http://cloud.friend.example/dav", "ftp://x/y", "not a url", ""):
        assert post(env, client, **{**GOOD, "url": url}).status_code == 400
    assert saved(env)[0] is None


def test_save_rejects_disallowed_kind_and_unknown_kind(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True, storage_allowed=("webdav",))
    assert post(env, client, kind="youtube").status_code == 400
    assert post(env, client, kind="local", url="/etc").status_code == 400
    assert post(env, client, kind="rclone", remote="mega").status_code == 400
    assert saved(env)[0] is None


def test_save_youtube_choice(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True, storage="webdav", storage_webdav_url="https://nc.example/dav")
    assert post(env, client, kind="youtube").status_code == 302
    assert saved(env)[0]["kind"] == "youtube"


def test_empty_password_keeps_old(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    post(env, client, **GOOD)
    post(env, client, **{**GOOD, "password": "", "folder": "Other"})
    config = json.loads(env.vault.decrypt(saved(env)[0]["config_enc"]))
    assert config["password"] == "S3cretPW" and config["folder"] == "Other"


def test_default_choice_deletes_override(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    post(env, client, **GOOD)
    assert post(env, client, kind="default").status_code == 302
    assert saved(env)[0] is None


class FakeStorage:
    def __init__(self, error=None):
        self.error = error

    def check(self):
        if self.error:
            raise StorageError(self.error)


def test_test_button_reports_success_and_error(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    built = []

    def builder(kind, config, ctx, host_check=None):
        built.append((kind, dict(config)))
        return FakeStorage(error=None if config["password"] == "S3cretPW" else "the WebDAV server refused the login")

    env.app.extensions["tapo"]["storage_builder"] = builder
    ok = post(env, client, "/storage/test", **GOOD)
    assert ok.status_code == 200 and "Works" in ok.get_data(as_text=True)
    bad = post(env, client, "/storage/test", **{**GOOD, "password": "wrong"})
    assert bad.status_code == 200 and "refused the login" in bad.get_data(as_text=True)
    assert built[0][0] == "webdav" and saved(env)[0] is None   # testing never saves


def test_test_button_uses_the_saved_password_when_left_empty(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    post(env, client, **GOOD)
    seen = []
    env.app.extensions["tapo"]["storage_builder"] = lambda kind, config, ctx, host_check=None: (seen.append(config["password"]), FakeStorage())[1]
    post(env, client, "/storage/test", **{**GOOD, "password": ""})
    assert seen == ["S3cretPW"]


def test_test_button_checks_the_address_first(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    env.app.extensions["tapo"]["storage_builder"] = lambda *a, **k: FakeStorage()
    response = post(env, client, "/storage/test", **{**GOOD, "url": "https://10.0.0.5/dav"})
    assert response.status_code == 400 and "non-public" in response.get_data(as_text=True)


def test_post_without_csrf_is_rejected(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True)
    assert client.post("/storage", data=GOOD).status_code in (400, 403)
    assert client.post("/storage/test", data=GOOD).status_code in (400, 403)
    assert saved(env)[0] is None


def test_notice_is_shown_when_override_was_ignored(tmp_path):
    env, client = setup(tmp_path, allow_own_storage=True, storage_allowed=("youtube",))
    uid = saved(env)[1]
    env.db.save_storage_config(uid, "webdav", env.vault.encrypt(json.dumps(GOOD)))
    page = client.get("/storage").get_data(as_text=True)
    assert "no longer allowed" in page


def test_nav_links_to_the_page(tmp_path):
    env, client = setup(tmp_path)
    assert 'href="/storage"' in client.get("/").get_data(as_text=True)
