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
