from datetime import time
from pathlib import Path

import pytest

from tapo_web.config import Settings, SettingsError
from tapo_web.crypto import Vault

GOOD_ENV = {"MULTIUSER": "1", "ENCRYPTION_KEY": Vault.generate_key(), "SECRET_KEY": "s3cret"}


def test_defaults():
    settings = Settings.from_env({})
    assert settings.multiuser is False
    assert settings.quota_daily == 10000
    assert settings.max_cameras_per_user == 1
    assert settings.max_cameras_total == 20
    assert settings.retention_days == 30
    assert settings.nextcloud_admin_group == "admin"
    assert settings.nextcloud_user_group is None
    assert settings.allow_private_hosts is False
    assert settings.allow_own_google_client is False
    assert settings.upload_at == time(21, 0)
    assert settings.local_login is True


def test_multiuser_requires_keys():
    with pytest.raises(SettingsError):
        Settings.from_env({"MULTIUSER": "1"})
    with pytest.raises(SettingsError):
        Settings.from_env({"MULTIUSER": "1", "ENCRYPTION_KEY": GOOD_ENV["ENCRYPTION_KEY"]})
    with pytest.raises(SettingsError):
        Settings.from_env({"MULTIUSER": "1", "SECRET_KEY": "x"})


def test_invalid_encryption_key_rejected():
    with pytest.raises(SettingsError):
        Settings.from_env({"MULTIUSER": "1", "ENCRYPTION_KEY": "not-a-key", "SECRET_KEY": "x"})


def test_single_user_needs_no_keys():
    assert Settings.from_env({"MULTIUSER": "0"}).multiuser is False


def test_values_are_read():
    settings = Settings.from_env({**GOOD_ENV, "NEXTCLOUD_USER_GROUP": "friends", "QUOTA_DAILY": "5000",
                                  "PORT_RANGE": "40000-40010", "ALLOW_PRIVATE_HOSTS": "true",
                                  "UPLOAD_AT": "22:30", "DATA_DIR": "/data"})
    assert settings.multiuser is True
    assert settings.nextcloud_user_group == "friends"
    assert settings.quota_daily == 5000
    assert settings.port_range == (40000, 40010)
    assert settings.allow_private_hosts is True
    assert settings.upload_at == time(22, 30)
    assert settings.data_dir == Path("/data")


def test_bad_port_range_rejected():
    with pytest.raises(SettingsError):
        Settings.from_env({**GOOD_ENV, "PORT_RANGE": "50000-40000"})
