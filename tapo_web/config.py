"""Settings of the multi-account mode, read from environment variables."""
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Mapping, Optional

from .crypto import Vault, VaultError


class SettingsError(ValueError):
    pass


STORAGE_KINDS = ("youtube", "webdav", "local", "rclone")
USER_STORAGE_KINDS = ("youtube", "webdav", "onedrive", "local")  # "local" for a user means the server's disk, with no path to pick;
# local and rclone are server-level only: a user could aim them at the server


def _flag(environ, name, default=False):
    value = environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _non_negative(environ, name, default, cast):
    try:
        value = cast(environ.get(name) or default)
    except ValueError as err:
        raise SettingsError(f"{name} must be a number") from err
    if value < 0:
        raise SettingsError(f"{name} cannot be negative")
    return value


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
    storage: str = "youtube"
    storage_allowed: tuple = USER_STORAGE_KINDS
    allow_own_storage: bool = False
    storage_webdav_url: str = ""
    storage_webdav_user: str = ""
    storage_webdav_password: str = ""
    storage_webdav_folder: str = ""
    storage_local_root: str = ""
    rclone_remote: str = ""
    rclone_config: str = ""
    rclone_folder: str = ""
    local_keep_days: int = 0          # 0 = keep forever; a user may choose their own
    local_quota_gb: float = 0         # per user, 0 = unlimited; an administrator may set one per user
    disk_min_free_gb: float = 1.0     # below this the page warns everybody and the oldest local videos go; 0 = off
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_tenant: str = "common"
    ms_graph_url: str = "https://graph.microsoft.com/v1.0"
    ms_login_url: str = "https://login.microsoftonline.com"

    @property
    def local_root(self):
        """Where videos kept on the server go (one sub-folder per user and camera)."""
        return self.storage_local_root or str(self.data_dir / "videos")

    @property
    def onedrive_enabled(self):
        return bool(self.ms_client_id and self.ms_client_secret)

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
        storage = (environ.get("STORAGE") or "youtube").strip().lower()
        if storage not in STORAGE_KINDS:
            raise SettingsError(f"STORAGE must be one of {', '.join(STORAGE_KINDS)}")
        allowed = tuple(kind.strip().lower() for kind in (environ.get("STORAGE_ALLOWED") or "youtube,webdav,onedrive").split(",")
                        if kind.strip())
        if not allowed or any(kind not in USER_STORAGE_KINDS for kind in allowed):
            raise SettingsError("STORAGE_ALLOWED may only list youtube, webdav, onedrive and local (rclone is server-level)")
        needs = {"webdav": "STORAGE_WEBDAV_URL", "local": "STORAGE_LOCAL_ROOT", "rclone": "STORAGE_RCLONE_REMOTE"}
        if storage in needs and not environ.get(needs[storage]):
            raise SettingsError(f"STORAGE={storage} needs {needs[storage]}")
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
            storage=storage,
            storage_allowed=allowed,
            allow_own_storage=_flag(environ, "ALLOW_OWN_STORAGE"),
            storage_webdav_url=environ.get("STORAGE_WEBDAV_URL", ""),
            storage_webdav_user=environ.get("STORAGE_WEBDAV_USER", ""),
            storage_webdav_password=environ.get("STORAGE_WEBDAV_PASSWORD", ""),
            storage_webdav_folder=environ.get("STORAGE_WEBDAV_FOLDER", ""),
            storage_local_root=environ.get("STORAGE_LOCAL_ROOT", ""),
            rclone_remote=environ.get("STORAGE_RCLONE_REMOTE", ""),
            rclone_config=environ.get("STORAGE_RCLONE_CONFIG", ""),
            rclone_folder=environ.get("STORAGE_RCLONE_FOLDER", ""),
            local_keep_days=_non_negative(environ, "LOCAL_KEEP_DAYS", 0, int),
            local_quota_gb=_non_negative(environ, "LOCAL_QUOTA_GB", 0, float),
            disk_min_free_gb=_non_negative(environ, "DISK_MIN_FREE_GB", 1.0, float),
            ms_client_id=environ.get("MS_CLIENT_ID", ""),
            ms_client_secret=environ.get("MS_CLIENT_SECRET", ""),
            ms_tenant=environ.get("MS_TENANT") or "common",
        )
