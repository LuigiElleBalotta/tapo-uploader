"""Single-user mode must keep working exactly as before, and the packaging must carry the new modules."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROGRAM_MODULES = ("tapo_uploader", "tapo_motion", "camera_context", "camera_worker")


def test_single_user_programs_do_not_need_flask(single_user_env, monkeypatch):
    for name in list(sys.modules):
        if name in PROGRAM_MODULES or name == "flask" or name.startswith(("flask.", "tapo_web")):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "flask", None)  # importing flask now raises ImportError
    import importlib
    uploader = importlib.import_module("tapo_uploader")
    motion = importlib.import_module("tapo_motion")
    importlib.import_module("camera_worker")
    for command in ("auth", "check", "test", "run"):
        assert command in uploader.__doc__
    for command in ("listen", "run", "upload"):
        assert command in motion.__doc__
        assert callable(getattr(motion, f"cmd_{command}"))
    monkeypatch.setattr(sys, "argv", ["tapo_uploader.py"])
    with pytest.raises(SystemExit) as stopped:
        uploader.main()
    assert "auth" in str(stopped.value)


def test_web_app_refuses_to_run_without_multiuser(tmp_path):
    from tapo_web.__main__ import main
    with pytest.raises(SystemExit) as stopped:
        main(["run"], environ={"DATA_DIR": str(tmp_path)})
    assert "MULTIUSER=1" in str(stopped.value)


def test_core_requirements_stay_light():
    core = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for heavy in ("flask", "waitress", "cryptography"):
        assert heavy not in core
    web = (ROOT / "requirements-web.txt").read_text(encoding="utf-8").lower()
    for needed in ("flask", "waitress", "cryptography", "requests", "tzdata"):
        assert needed in web


def test_dockerfile_carries_every_module():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for needed in ("camera_context.py", "camera_worker.py", "tapo_uploader.py", "tapo_motion.py", "tapo_web",
                   "requirements-web.txt"):
        assert needed in dockerfile, needed


def test_every_setting_is_documented_in_env_example():
    config = (ROOT / "tapo_web" / "config.py").read_text(encoding="utf-8")
    names = set(re.findall(r'(?:environ\.get|_flag|_int)\((?:environ, )?"([A-Z_]+)"', config))
    assert {"MULTIUSER", "ENCRYPTION_KEY", "SECRET_KEY", "NEXTCLOUD_URL", "QUOTA_DAILY"} <= names
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    missing = sorted(name for name in names if name not in example)
    assert not missing, f"undocumented in .env.example: {missing}"


def test_systemd_unit_for_the_web_app():
    unit = (ROOT / "deploy" / "tapo-web.service").read_text(encoding="utf-8")
    assert "python -u -m tapo_web run" in unit and "Restart=always" in unit and "Environment=TZ=" in unit
    assert "EnvironmentFile=" in unit


def test_readmes_document_multi_account():
    for name in ("README.md", "README_IT.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for needed in ("MULTIUSER", "ENCRYPTION_KEY", "NEXTCLOUD_URL", "/youtube/callback", "python -m tapo_web"):
            assert needed in text, f"{name} lacks {needed}"
