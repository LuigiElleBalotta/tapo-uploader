from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from tapo_web.auth_nextcloud import fetch_nextcloud_user, role_from_groups
from tests.conftest import Env, make_settings

NEXTCLOUD = dict(nextcloud_url="https://cloud.example.com", nextcloud_client_id="client-id",
                 nextcloud_client_secret="client-secret", nextcloud_user_group="friends",
                 public_url="http://tapo.example.com")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeNextcloud:
    """Stands in for `requests`: the token endpoint and the OCS user endpoint."""

    def __init__(self, groups, uid="alice", name="Alice A.", fail=None):
        self.groups, self.uid, self.name, self.fail = groups, uid, name, fail
        self.calls = []

    def post(self, url, data=None, timeout=None, **kwargs):
        self.calls.append(("post", url, data))
        if self.fail:
            raise self.fail
        return FakeResponse({"access_token": "tok", "token_type": "Bearer", "user_id": self.uid})

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(("get", url, headers))
        return FakeResponse({"ocs": {"meta": {"statuscode": 200},
                                     "data": {"id": self.uid, "displayname": self.name, "groups": self.groups}}})


def state_of(client, next_url=""):
    response = client.get("/auth/nextcloud/start", query_string={"next": next_url} if next_url else None)
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["Location"]).query)["state"][0], response.headers["Location"]


def finish(client, state, code="the-code"):
    return client.get("/auth/nextcloud/callback", query_string={"code": code, "state": state})


def configured(tmp_path, groups, **kwargs):
    env = Env(tmp_path, **NEXTCLOUD)
    fake = FakeNextcloud(groups, **kwargs)
    env.app.extensions["tapo"]["http"] = fake
    return env, fake


# ----- roles -----

def test_admin_group_gives_admin():
    assert role_from_groups(["admin", "friends"], "admin", "friends") == "admin"


def test_user_group_gives_user():
    assert role_from_groups(["friends"], "admin", "friends") == "user"


def test_admin_wins_over_user():
    assert role_from_groups(["friends", "admin"], "admin", "friends") == "admin"


def test_no_user_group_configured_only_admins_enter():
    assert role_from_groups(["friends"], "admin", None) is None
    assert role_from_groups(["admin"], "admin", None) == "admin"


def test_group_names_are_case_sensitive():
    assert role_from_groups(["Admin"], "admin", "friends") is None
    assert role_from_groups(["FRIENDS"], "admin", "friends") is None


@pytest.mark.parametrize("groups", [[], None])
def test_empty_groups_denied(groups):
    assert role_from_groups(groups, "admin", "friends") is None


def test_fetch_user_reads_ocs_payload(tmp_path):
    http = FakeNextcloud(["friends"])
    user = fetch_nextcloud_user(make_settings(tmp_path, **NEXTCLOUD), "tok", http=http)
    assert user == {"id": "alice", "display_name": "Alice A.", "groups": ["friends"]}
    method, url, headers = http.calls[0]
    assert url == "https://cloud.example.com/ocs/v2.php/cloud/user?format=json"
    assert headers["Authorization"] == "Bearer tok" and headers["OCS-APIRequest"] == "true"


# ----- the login flow -----

def test_start_is_404_when_not_configured(web):
    assert web.client.get("/auth/nextcloud/start").status_code == 404
    assert "Nextcloud" not in web.client.get("/login").get_data(as_text=True)


def test_login_page_offers_nextcloud_when_configured(tmp_path):
    env = Env(tmp_path, **NEXTCLOUD)
    assert "Sign in with Nextcloud" in env.client.get("/login").get_data(as_text=True)


def test_start_redirects_to_nextcloud_with_state(tmp_path):
    env = Env(tmp_path, **NEXTCLOUD)
    _, location = state_of(env.client)
    query = parse_qs(urlsplit(location).query)
    assert location.startswith("https://cloud.example.com/index.php/apps/oauth2/authorize?")
    assert query["client_id"] == ["client-id"] and query["response_type"] == ["code"]
    assert query["redirect_uri"] == ["http://tapo.example.com/auth/nextcloud/callback"]


def test_callback_creates_user_with_role(tmp_path):
    env, _ = configured(tmp_path, ["friends"])
    response = finish(env.client, state_of(env.client)[0])
    assert response.status_code == 302 and response.headers["Location"] == "/"
    user = env.db.user_by_username("nextcloud", "alice")
    assert user["role"] == "user" and user["display_name"] == "Alice A." and user["active"] == 1
    assert "Alice A." in env.client.get("/").get_data(as_text=True)


def test_admin_group_member_becomes_admin(tmp_path):
    env, _ = configured(tmp_path, ["admin"])
    finish(env.client, state_of(env.client)[0])
    assert env.db.user_by_username("nextcloud", "alice")["role"] == "admin"


def test_role_re_evaluated_each_login(tmp_path):
    env, fake = configured(tmp_path, ["friends"])
    finish(env.client, state_of(env.client)[0])
    uid = env.db.user_by_username("nextcloud", "alice")["id"]
    fake.groups = []  # removed from the group in Nextcloud
    other = env.app.test_client()
    response = finish(other, state_of(other)[0])
    assert response.status_code == 403
    assert env.db.user_by_id(uid)["active"] == 0
    assert other.get("/").status_code == 302  # not signed in


def test_user_disabled_by_admin_stays_out_even_in_group(tmp_path):
    env, _ = configured(tmp_path, ["friends"])
    finish(env.client, state_of(env.client)[0])
    env.db.set_user_active(env.db.user_by_username("nextcloud", "alice")["id"], False)
    other = env.app.test_client()
    assert finish(other, state_of(other)[0]).status_code == 403


def test_state_mismatch_rejected(tmp_path):
    env, _ = configured(tmp_path, ["friends"])
    state_of(env.client)
    assert finish(env.client, "not-the-state").status_code == 400
    assert env.db.user_by_username("nextcloud", "alice") is None


def test_callback_without_start_rejected(tmp_path):
    env, _ = configured(tmp_path, ["friends"])
    assert finish(env.client, "anything").status_code == 400


def test_nextcloud_error_shows_message_not_traceback(tmp_path):
    env, _ = configured(tmp_path, ["friends"], fail=requests.ConnectionError("boom host 10.1.2.3"))
    response = finish(env.client, state_of(env.client)[0])
    body = response.get_data(as_text=True)
    assert response.status_code == 502
    assert "Nextcloud" in body and "Traceback" not in body and "10.1.2.3" not in body


def test_denied_by_nextcloud_shows_message(tmp_path):
    env, _ = configured(tmp_path, ["friends"])
    state_of(env.client)
    response = env.client.get("/auth/nextcloud/callback", query_string={"error": "access_denied", "state": "x"})
    assert response.status_code == 400 and "Traceback" not in response.get_data(as_text=True)


def test_next_is_kept_only_if_local(tmp_path):
    env, _ = configured(tmp_path, ["friends"])
    response = finish(env.client, state_of(env.client, "https://evil.example.com")[0])
    assert response.headers["Location"] == "/"


def test_next_local_path_is_kept(tmp_path):
    env, _ = configured(tmp_path, ["friends"])
    response = finish(env.client, state_of(env.client, "/cameras/new")[0])
    assert response.headers["Location"] == "/cameras/new"
