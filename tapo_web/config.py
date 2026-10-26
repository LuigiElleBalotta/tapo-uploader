"""Settings of the multi-account mode, read from environment variables."""
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Mapping, Optional

from .crypto import Vault, VaultError


class SettingsError(ValueError):
    pass


def _flag(environ, name, default=False):
    value = environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(environ, name, default):
    try:
        return int(environ.get(name) or default)
    except ValueError as err:
        raise SettingsError(f"{name} must be a number") from err


@dataclass(frozen=True)
class Settings:
    multiuser: bool = False
    public_url: str = ""
    secret_key: str = ""
    encryption_key: str = ""
    server_public_ip: str = ""
    port_range: tuple = (30000, 39999)
    data_dir: Path = Path(".")
    nextcloud_url: str = ""
    nextcloud_client_id: str = ""
    nextcloud_client_secret: str = ""
    nextcloud_admin_group: str = "admin"
    nextcloud_user_group: Optional[str] = None
    google_client_json: Optional[str] = None
    allow_own_google_client: bool = False
    quota_daily: int = 10000
    upload_at: time = time(21, 0)
    upload_stagger_minutes: int = 10
    max_cameras_per_user: int = 1
    max_cameras_total: int = 20
    retention_days: int = 30
    allow_private_hosts: bool = False
    local_login: bool = True
    trusted_proxy_hops: int = 0

    @property
    def nextcloud_enabled(self):
        return bool(self.nextcloud_url and self.nextcloud_client_id and self.nextcloud_client_secret)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]):
        multiuser = _flag(environ, "MULTIUSER")
        encryption_key = environ.get("ENCRYPTION_KEY", "")
        secret_key = environ.get("SECRET_KEY", "")
        if multiuser:
            if not encryption_key or not secret_key:
                raise SettingsError("MULTIUSER=1 needs ENCRYPTION_KEY and SECRET_KEY")
            try:
                Vault(encryption_key)
            except VaultError as err:
                raise SettingsError("ENCRYPTION_KEY is not a valid key (generate one with "
                                    "python -c \"from tapo_web.crypto import Vault; print(Vault.generate_key())\")") from err
        try:
            start, end = (int(part) for part in (environ.get("PORT_RANGE") or "30000-39999").split("-"))
        except ValueError as err:
            raise SettingsError("PORT_RANGE must look like 30000-39999") from err
        if not 1024 <= start <= end <= 65535:
            raise SettingsError("PORT_RANGE must be between 1024 and 65535, start before end")
        try:
            upload_at = datetime.strptime(environ.get("UPLOAD_AT") or "21:00", "%H:%M").time()
        except ValueError as err:
            raise SettingsError("UPLOAD_AT must look like 21:00") from err
        client_json = environ.get("YOUTUBE_CLIENT_SECRET_JSON") or None
        return cls(
            multiuser=multiuser,
            public_url=(environ.get("PUBLIC_URL") or "").rstrip("/"),
            secret_key=secret_key,
            encryption_key=encryption_key,
            server_public_ip=environ.get("SERVER_PUBLIC_IP", ""),
            port_range=(start, end),
            data_dir=Path(environ.get("DATA_DIR") or "."),
            nextcloud_url=(environ.get("NEXTCLOUD_URL") or "").rstrip("/"),
            nextcloud_client_id=environ.get("NEXTCLOUD_CLIENT_ID", ""),
            nextcloud_client_secret=environ.get("NEXTCLOUD_CLIENT_SECRET", ""),
            nextcloud_admin_group=environ.get("NEXTCLOUD_ADMIN_GROUP") or "admin",
            nextcloud_user_group=environ.get("NEXTCLOUD_USER_GROUP") or None,
            google_client_json=client_json,
            allow_own_google_client=_flag(environ, "ALLOW_OWN_GOOGLE_CLIENT"),
            quota_daily=_int(environ, "QUOTA_DAILY", 10000),
            upload_at=upload_at,
            upload_stagger_minutes=_int(environ, "UPLOAD_STAGGER_MINUTES", 10),
            max_cameras_per_user=_int(environ, "MAX_CAMERAS_PER_USER", 1),
            max_cameras_total=_int(environ, "MAX_CAMERAS_TOTAL", 20),
            retention_days=_int(environ, "RETENTION_DAYS", 30),
            allow_private_hosts=_flag(environ, "ALLOW_PRIVATE_HOSTS"),
            local_login=_flag(environ, "LOCAL_LOGIN", True),
            trusted_proxy_hops=_int(environ, "TRUSTED_PROXY_HOPS", 0),
        )
