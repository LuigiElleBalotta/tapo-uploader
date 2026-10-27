"""Connecting OneDrive from the Recordings page, and using it as a destination."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from tapo_web.cameras import camera_data_dir
from tapo_web.config import Settings
from tapo_web.storage import build, resolve
from tests.conftest import Env
from tests.fakes_graph import FakeGraph

DAY = "2026-10-26"


@pytest.fixture
def graph():
    server = FakeGraph()
    yield server
    server.stop()


def setup(tmp_path, graph, **settings):
    values = dict(allow_own_storage=True, ms_client_id="cid", ms_client_secret="csecret", public_url="https://cam.example",
                  ms_graph_url=graph.graph_url, ms_login_url=graph.base)
    values.update(settings)
    env = Env(tmp_path, **values)
    env.alice = env.add_local_user("alice", "correct horse")
    env.cid = env.db.add_camera(env.alice, "Garden", "cam.example.com", 38554, 38020, "stream1",
                                env.vault.encrypt("u"), env.vault.encrypt("p"))
    env.client = env.app.test_client()
    env.login("alice", "correct horse", client=env.client)
    return env


def connect(env):
    response = env.client.post("/storage/onedrive/connect", data={"csrf_token": env.csrf(env.client, "/storage")})
    return response


def finish(env, location, code="good-code", **extra):
    query = parse_qs(urlsplit(location).query)
    return env.client.get("/storage/onedrive/callback", query_string={"code": code, "state": query["state"][0], **extra})


def test_settings_read_the_microsoft_app_and_default_to_allowing_onedrive():
    settings = Settings.from_env({"MS_CLIENT_ID": "id", "MS_CLIENT_SECRET": "secret", "MS_TENANT": "consumers"})
    assert settings.onedrive_enabled and settings.ms_tenant == "consumers"
    assert not Settings.from_env({}).onedrive_enabled and Settings.from_env({}).ms_tenant == "common"
    assert "onedrive" in Settings.from_env({}).storage_allowed


def test_page_offers_connect_only_when_the_server_has_a_microsoft_app(tmp_path, graph):
    env = setup(tmp_path, graph)
    assert "Connect OneDrive" in env.client.get("/storage").get_data(as_text=True)
    bare = setup(tmp_path / "b", graph, ms_client_id="", ms_client_secret="")
    assert "Connect OneDrive" not in bare.client.get("/storage").get_data(as_text=True)


def test_connect_sends_the_user_to_microsoft_with_state_and_pkce(tmp_path, graph):
    env = setup(tmp_path, graph)
    response = connect(env)
    assert response.status_code == 302
    url = urlsplit(response.headers["Location"])
    assert f"http://127.0.0.1:{graph.port}/common/oauth2/v2.0/authorize" == f"{url.scheme}://{url.netloc}{url.path}"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert query["client_id"] == "cid" and query["response_type"] == "code"
    assert query["redirect_uri"] == "https://cam.example/storage/onedrive/callback"
    assert query["scope"] == "Files.ReadWrite.AppFolder offline_access" and query["code_challenge_method"] == "S256"
    assert len(query["state"]) >= 20 and len(query["code_challenge"]) >= 40 and "client_secret" not in url.query


def test_connect_needs_csrf_permission_and_a_configured_app(tmp_path, graph):
    env = setup(tmp_path, graph)
    assert env.client.post("/storage/onedrive/connect").status_code == 400
    off = setup(tmp_path / "o", graph, allow_own_storage=False)
    assert off.client.post("/storage/onedrive/connect", data={"csrf_token": off.csrf(off.client, "/storage")}).status_code == 403
    bare = setup(tmp_path / "b", graph, ms_client_id="", ms_client_secret="")
    assert bare.client.post("/storage/onedrive/connect", data={"csrf_token": bare.csrf(bare.client, "/storage")}).status_code == 503
    not_allowed = setup(tmp_path / "n", graph, storage_allowed=("youtube", "webdav"))
    assert not_allowed.client.post("/storage/onedrive/connect",
                                   data={"csrf_token": not_allowed.csrf(not_allowed.client, "/storage")}).status_code == 400


def test_callback_saves_the_connection_encrypted_and_remembers_the_old_destination(tmp_path, graph):
    env = setup(tmp_path, graph)
    response = finish(env, connect(env).headers["Location"])
    assert response.status_code == 302
    row = env.db.storage_config(env.alice)
    assert row["kind"] == "onedrive" and "refresh-1" not in row["config_enc"]
    resolved = resolve(env.settings, env.db, env.vault, env.alice)
    assert resolved.kind == "onedrive" and resolved.source == "own" and resolved.config["refresh_token"] == "refresh-1"
    assert resolved.config["account"] == "drive-1"
    assert [(r["kind"], r["origin"]) for r in env.db.storage_sources(env.alice)] == [("youtube", "server")]


@pytest.mark.parametrize("how", ["wrong-state", "error", "bad-code", "no-session"])
def test_callback_refuses_what_it_cannot_trust_and_saves_nothing(tmp_path, graph, how):
    env = setup(tmp_path, graph)
    location = connect(env).headers["Location"]
    if how == "wrong-state":
        response = env.client.get("/storage/onedrive/callback", query_string={"code": "good-code", "state": "x" * 30})
    elif how == "error":
        response = env.client.get("/storage/onedrive/callback", query_string={"error": "access_denied", "state": parse_qs(urlsplit(location).query)["state"][0]})
    elif how == "bad-code":
        response = finish(env, location, code="bad")
    else:
        response = env.app.test_client().get("/storage/onedrive/callback", query_string={"code": "good-code", "state": "y" * 30})
    assert response.status_code in (302, 400, 502) and (response.status_code != 302 or "/login" in response.headers["Location"])
    assert env.db.storage_config(env.alice) is None
    assert "AADSTS" not in response.get_data(as_text=True)


def test_state_works_once_only(tmp_path, graph):
    env = setup(tmp_path, graph)
    location = connect(env).headers["Location"]
    assert finish(env, location).status_code == 302
    assert finish(env, location).status_code == 400


def onedrive_env(tmp_path, graph, **kw):
    env = setup(tmp_path, graph, **kw)
    finish(env, connect(env).headers["Location"])
    return env


def storage_for(env):
    camera = env.db.camera_by_id(env.cid)
    ctx = type("Ctx", (), {"data_dir": camera_data_dir(env.settings, camera), "playlist_prefix": "Tapo"})()
    return build(resolve(env.settings, env.db, env.vault, env.alice), env.settings, ctx, env.app.extensions["tapo"]["resolver"])


def test_a_rotated_refresh_token_is_saved_for_next_time(tmp_path, graph):
    env = onedrive_env(tmp_path, graph)
    graph.rotate = True
    storage_for(env).check()
    saved = json.loads(env.vault.decrypt(env.db.storage_config(env.alice)["config_enc"]))
    assert saved["refresh_token"] == graph.refresh_token != "refresh-1" and saved["account"] == "drive-1"
    storage_for(env).check()                                   # the new token works


def test_the_destination_signature_ignores_token_rotation_but_not_the_drive(tmp_path, graph):
    env = onedrive_env(tmp_path, graph)
    before = resolve(env.settings, env.db, env.vault, env.alice).signature()
    graph.rotate = True
    storage_for(env).check()
    assert resolve(env.settings, env.db, env.vault, env.alice).signature() == before
    config = json.loads(env.vault.decrypt(env.db.storage_config(env.alice)["config_enc"]))
    env.db.save_storage_config(env.alice, "onedrive", env.vault.encrypt(json.dumps({**config, "account": "drive-2"})))
    assert resolve(env.settings, env.db, env.vault, env.alice).signature() != before


def test_videos_in_onedrive_show_on_the_page_with_a_badge_and_play_through_media(tmp_path, graph):
    env = onedrive_env(tmp_path, graph)
    clip = tmp_path / "src.mp4"
    clip.write_bytes(b"0123456789")
    storage_for(env).put(clip, DAY, "from onedrive", "")
    text = env.client.get("/").get_data(as_text=True)
    assert "from onedrive" in text and '<span class="badge badge-source">OneDrive</span>' in text
    name = "from%20onedrive.mp4"
    assert env.client.get(f"/media/{env.cid}/{DAY}/{name}").data == b"0123456789"
    assert env.client.get(f"/media/{env.cid}/{DAY}/{name}", headers={"Range": "bytes=2-5"}).data == b"2345"


def test_if_the_admin_stops_allowing_onedrive_the_server_default_applies_with_a_notice(tmp_path, graph):
    env = onedrive_env(tmp_path, graph)
    stricter = Settings(**{**env.settings.__dict__, "storage_allowed": ("youtube", "webdav")})
    resolved = resolve(stricter, env.db, env.vault, env.alice)
    assert resolved.kind == "youtube" and resolved.notice and "no longer allowed" in resolved.notice


def test_if_the_microsoft_app_is_removed_from_the_server_the_default_applies_with_a_notice(tmp_path, graph):
    env = onedrive_env(tmp_path, graph)
    bare = Settings(**{**env.settings.__dict__, "ms_client_id": "", "ms_client_secret": ""})
    resolved = resolve(bare, env.db, env.vault, env.alice)
    assert resolved.kind == "youtube" and resolved.notice
