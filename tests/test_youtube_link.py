import json
from urllib.parse import parse_qs, urlsplit

import pytest
from google.auth.exceptions import RefreshError

from tapo_web.youtube_link import LinkRevoked, client_json_for, credentials_for, youtube_for_user
from tests.conftest import Env

SHARED = json.dumps({"web": {"client_id": "shared-id", "client_secret": "shared-secret",
                             "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                             "token_uri": "https://oauth2.googleapis.com/token"}})
OWN = json.dumps({"web": {"client_id": "own-id", "client_secret": "own-secret",
                          "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                          "token_uri": "https://oauth2.googleapis.com/token"}})
PUBLIC = "http://tapo.example.com"


def token_json(expiry="2099-01-01T00:00:00Z", token="access", refresh="refresh"):
    return json.dumps({"token": token, "refresh_token": refresh, "client_id": "shared-id",
                       "client_secret": "shared-secret", "token_uri": "https://oauth2.googleapis.com/token",
                       "scopes": ["https://www.googleapis.com/auth/youtube"], "expiry": expiry})


def make_env(tmp_path, **kwargs):
    env = Env(tmp_path, public_url=PUBLIC, google_client_json=SHARED, **kwargs)
    env.alice = env.add_local_user("alice", "correct horse")
    env.bob = env.add_local_user("bob", "another pass")
    return env


def link(env, user_id, token=None, source="shared", client_secret=None):
    env.db.save_youtube_link(user_id, env.vault.encrypt(token or token_json()), "Alice channel", source,
                             env.vault.encrypt(client_secret) if client_secret else None)


class FakeCredentials:
    def __init__(self, refresh_token="refresh"):
        self.refresh_token = refresh_token

    def to_json(self):
        return token_json()


class FakeFlow:
    code_verifier = "verifier"

    def __init__(self, refresh_token="refresh"):
        self.credentials = FakeCredentials(refresh_token)
        self.fetched = None

    def authorization_url(self, **kwargs):
        return "https://accounts.google.com/o/oauth2/auth?state=fake-state", "fake-state"

    def fetch_token(self, code):
        self.fetched = code


class FakeChannels:
    def list(self, **kwargs):
        return self

    def execute(self):
        return {"items": [{"snippet": {"title": "My Channel"}}]}


class FakeYoutube:
    def channels(self):
        return FakeChannels()


def start(env, client, **form):
    csrf = env.csrf(client, "/youtube")
    return client.post("/youtube/connect", data={"csrf_token": csrf, **form})


# ----- helpers -----

def test_client_json_shared_by_default(tmp_path):
    env = make_env(tmp_path)
    assert client_json_for(env.settings, env.vault, None) == SHARED
    link(env, env.alice)
    assert client_json_for(env.settings, env.vault, env.db.youtube_link(env.alice)) == SHARED


def test_own_client_used_only_when_allowed(tmp_path):
    allowed = make_env(tmp_path / "a", allow_own_google_client=True)
    link(allowed, allowed.alice, source="own", client_secret=OWN)
    assert client_json_for(allowed.settings, allowed.vault, allowed.db.youtube_link(allowed.alice)) == OWN
    denied = make_env(tmp_path / "b", allow_own_google_client=False)
    link(denied, denied.alice, source="own", client_secret=OWN)
    assert client_json_for(denied.settings, denied.vault, denied.db.youtube_link(denied.alice)) == SHARED


def test_credentials_roundtrip(tmp_path):
    env = make_env(tmp_path)
    link(env, env.alice)
    creds = credentials_for(env.vault, env.db.youtube_link(env.alice))
    assert creds.token == "access" and creds.refresh_token == "refresh" and not creds.expired


def test_youtube_for_user_none_when_not_linked(tmp_path):
    env = make_env(tmp_path)
    link(env, env.alice)
    assert youtube_for_user(env.db, env.vault, env.settings, env.alice) is not None
    assert youtube_for_user(env.db, env.vault, env.settings, env.bob) is None


def test_refresh_failure_raises_link_revoked(tmp_path):
    env = make_env(tmp_path)
    link(env, env.alice, token=token_json(expiry="2000-01-01T00:00:00Z"))

    def refuse(creds):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    with pytest.raises(LinkRevoked):
        youtube_for_user(env.db, env.vault, env.settings, env.alice, refresher=refuse)


def test_refreshed_token_is_saved_encrypted(tmp_path):
    env = make_env(tmp_path)
    link(env, env.alice, token=token_json(expiry="2000-01-01T00:00:00Z", token="old"))

    def refresh(creds):
        creds.token = "new"
        from datetime import datetime, timedelta, timezone
        creds.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)  # google-auth wants naive UTC

    assert youtube_for_user(env.db, env.vault, env.settings, env.alice, refresher=refresh) is not None
    stored = env.db.youtube_link(env.alice)["token_enc"]
    assert "new" not in stored
    assert json.loads(env.vault.decrypt(stored))["token"] == "new"


# ----- routes -----

def test_connect_redirects_with_state_and_pkce(tmp_path):
    env = make_env(tmp_path)
    env.login("alice", "correct horse")
    response = start(env, env.client)
    assert response.status_code == 302
    location = response.headers["Location"]
    query = parse_qs(urlsplit(location).query)
    assert location.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert query["client_id"] == ["shared-id"] and query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"] and query["redirect_uri"] == [PUBLIC + "/youtube/callback"]
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"][0]
    with env.client.session_transaction() as session:
        assert session["yt_state"] == query["state"][0] and session["yt_verifier"]


def test_connect_requires_login_and_csrf(tmp_path):
    env = make_env(tmp_path)
    assert env.client.post("/youtube/connect", data={}).status_code == 400
    env.login("alice", "correct horse")
    assert env.client.post("/youtube/connect", data={}).status_code == 400


def test_callback_stores_encrypted_token_and_channel_title(tmp_path):
    env = make_env(tmp_path)
    env.login("alice", "correct horse")
    fake = FakeFlow()
    state = env.app.extensions["tapo"]
    state["flow_factory"] = lambda settings, redirect_uri, client_json, code_verifier=None: fake
    state["youtube_builder"] = lambda credentials: FakeYoutube()
    start(env, env.client)
    with env.client.session_transaction() as session:
        session["yt_state"] = "fake-state"
    response = env.client.get("/youtube/callback", query_string={"code": "abc", "state": "fake-state"})
    assert response.status_code == 302 and response.headers["Location"] == "/youtube"
    assert fake.fetched == "abc"
    row = env.db.youtube_link(env.alice)
    assert row["channel_title"] == "My Channel" and row["client_source"] == "shared"
    assert "access" not in row["token_enc"] and json.loads(env.vault.decrypt(row["token_enc"]))["refresh_token"] == "refresh"


def test_callback_rejects_wrong_state(tmp_path):
    env = make_env(tmp_path)
    env.login("alice", "correct horse")
    start(env, env.client)
    response = env.client.get("/youtube/callback", query_string={"code": "abc", "state": "wrong"})
    assert response.status_code == 400 and env.db.youtube_link(env.alice) is None


def test_callback_without_refresh_token_is_refused(tmp_path):
    env = make_env(tmp_path)
    env.login("alice", "correct horse")
    state = env.app.extensions["tapo"]
    state["flow_factory"] = lambda *args, **kwargs: FakeFlow(refresh_token=None)
    state["youtube_builder"] = lambda credentials: FakeYoutube()
    start(env, env.client)
    with env.client.session_transaction() as session:
        session["yt_state"] = "fake-state"
    response = env.client.get("/youtube/callback", query_string={"code": "abc", "state": "fake-state"})
    assert response.status_code == 400 and env.db.youtube_link(env.alice) is None


def test_disconnect_deletes_token(tmp_path):
    env = make_env(tmp_path)
    link(env, env.alice)
    env.login("alice", "correct horse")
    response = env.client.post("/youtube/disconnect", data={"csrf_token": env.csrf(path="/youtube")})
    assert response.status_code == 302 and env.db.youtube_link(env.alice) is None


def test_user_cannot_see_other_users_link(tmp_path):
    env = make_env(tmp_path)
    link(env, env.alice)
    env.login("bob", "another pass")
    page = env.client.get("/youtube").get_data(as_text=True)
    assert "Alice channel" not in page and "Connect" in page
    env.client.post("/youtube/disconnect", data={"csrf_token": env.csrf(path="/youtube")})
    assert env.db.youtube_link(env.alice) is not None  # bob's disconnect touched nothing of alice's


def test_connect_with_own_client_only_when_allowed(tmp_path):
    denied = make_env(tmp_path / "a")
    denied.login("alice", "correct horse")
    assert start(denied, denied.client, client_json=OWN).status_code == 403
    allowed = make_env(tmp_path / "b", allow_own_google_client=True)
    allowed.login("alice", "correct horse")
    response = start(allowed, allowed.client, client_json=OWN)
    assert parse_qs(urlsplit(response.headers["Location"]).query)["client_id"] == ["own-id"]


def test_own_client_json_is_validated(tmp_path):
    env = make_env(tmp_path, allow_own_google_client=True)
    env.login("alice", "correct horse")
    assert start(env, env.client, client_json="not json").status_code == 400
    assert start(env, env.client, client_json='{"web": {}}').status_code == 400
