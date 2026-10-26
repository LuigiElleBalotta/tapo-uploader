import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SINGLE_USER_ENV = {
    "CAMERA_USER": "cam",
    "CAMERA_PASS": "p@ss:w!rd",
    "CAMERA_HOST": "192.168.1.37",
    "CAMERA_PORT": "554",
    "CAMERA_STREAM": "stream1",
}


@pytest.fixture
def single_user_env(tmp_path, monkeypatch):
    """Single-user environment: the camera variables and a private DATA_DIR, with the two
    programs imported fresh so their module-level settings see it."""
    for name in list(sys.modules):
        if name in ("tapo_uploader", "tapo_motion", "camera_context", "camera_worker"):
            del sys.modules[name]
    for key, value in SINGLE_USER_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for key in ("ONVIF_HOST", "ONVIF_PORT", "UPLOAD_AT", "MOTION_EVENTS", "MOTION_PRE_SECONDS",
                "MOTION_POST_SECONDS", "MOTION_EXTEND_WITHIN", "BUFFER_SEGMENT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    uploader = importlib.import_module("tapo_uploader")
    motion = importlib.import_module("tapo_motion")
    yield SimpleNamespace(uploader=uploader, motion=motion, data=tmp_path)
    for name in ("tapo_uploader", "tapo_motion"):
        sys.modules.pop(name, None)


# ---------- multi-account web app helpers ----------

import re as _re

from tapo_web.config import Settings
from tapo_web.crypto import Vault
from tapo_web.db import Database


def make_settings(tmp_path, **overrides):
    values = dict(multiuser=True, secret_key="test-secret", encryption_key=Vault.generate_key(),
                  data_dir=tmp_path, server_public_ip="203.0.113.7")
    values.update(overrides)
    return Settings(**values)


class Env:
    """A running app with a database, a vault and a test client."""

    def __init__(self, tmp_path, **settings):
        from tapo_web.app import create_app
        self.settings = make_settings(tmp_path, **settings)
        self.db = Database(tmp_path / "app.db")
        self.db.migrate()
        self.vault = Vault(self.settings.encryption_key)
        self.app = create_app(self.settings, self.db)
        self.app.testing = True
        self.client = self.app.test_client()

    def add_local_user(self, username, password, role="user", **kw):
        from tapo_web.auth_local import hash_password
        return self.db.add_user("local", username, kw.get("display_name", username.title()), role,
                                hash_password(password))

    def csrf(self, client=None, path="/login"):
        page = (client or self.client).get(path).get_data(as_text=True)
        match = _re.search(r'name="csrf_token" value="([^"]+)"', page)
        assert match, f"no csrf token on {path}"
        return match[1]

    def login(self, username, password, client=None, **extra):
        client = client or self.client
        return client.post("/login", data={"username": username, "password": password,
                                           "csrf_token": self.csrf(client), **extra})


@pytest.fixture
def web(tmp_path):
    return Env(tmp_path)
