from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _runtime_root() -> Path:
    """Return the source tree or PyInstaller extraction root."""

    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[2]


def _packaged_data_dir(platform: str, environ: dict[str, str], home: Path) -> Path:
    if platform == "win32":
        base = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return base / "vFleet"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "vFleet"
    base = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return base / "vfleet"


ROOT = _runtime_root()
PACKAGED = bool(getattr(sys, "frozen", False))
DEFAULT_DATA_DIR = _packaged_data_dir(sys.platform, os.environ, Path.home()) if PACKAGED else ROOT / "data"
DEFAULT_ENV_FILE = DEFAULT_DATA_DIR / ".env" if PACKAGED else ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_mode: str = "auto"
    app_host: str = "127.0.0.1"
    app_port: int = 8080
    ui_token: str = ""

    vcenter_host: str = ""
    vcenter_user: str = ""
    vcenter_password: str = ""
    vcenter_port: int = 443
    vcenter_insecure: bool = True

    # Optional, tightly scoped SSH fallback for standalone ESXi disk conversion.
    esxi_ssh_enabled: bool = False
    esxi_ssh_user: str = ""
    esxi_ssh_password: str = ""
    esxi_ssh_port: int = Field(default=22, ge=1, le=65535)
    esxi_ssh_key_path: Path = Path("")
    esxi_ssh_host_key_sha256: str = ""
    esxi_ssh_timeout_seconds: int = Field(default=30, ge=5, le=300)
    esxi_ssh_use_login_password: bool = False

    # Runtime-only access path loaded from a connection profile. The jump
    # password is resolved from the Automation Vault and is never profile metadata.
    access_jump_enabled: bool = False
    access_jump_address: str = ""
    access_jump_port: int = Field(default=22, ge=1, le=65535)
    access_jump_host_type: Literal["auto", "windows", "unix"] = "auto"
    access_jump_user: str = ""
    access_jump_password: str = ""
    access_jump_host_key_sha256: str = ""

    name_group_pattern: str = r"^([A-Za-z][A-Za-z0-9]+)"
    owner_field_names: str = "Owner,owner,User,user,CreatedBy,createdBy"

    idle_days: int = 14
    cpu_idle_pct: float = 8.0
    memory_idle_pct: float = 20.0
    event_lookback_days: int = 30

    cache_ttl_seconds: int = Field(default=20, ge=5, le=300)
    data_dir: Path = Field(default=DEFAULT_DATA_DIR)
    relay_tick_seconds: float = Field(default=1.0, ge=0.2, le=30)
    sync_interval_seconds: int = Field(default=30, ge=10, le=600)
    connect_timeout_seconds: int = Field(default=20, ge=5, le=120)
    upload_chunk_bytes: int = Field(default=4 * 1024 * 1024, ge=256 * 1024)

    # Portable encrypted JSON credential vault. The key is kept outside DATA_DIR
    # by default and can be injected by a service/container at runtime.
    vfleet_master_key_file: str = ""
    vfleet_master_key: SecretStr = SecretStr("")

    @property
    def credential_key_file(self) -> Path:
        if self.vfleet_master_key_file.strip():
            return Path(self.vfleet_master_key_file).expanduser()
        return Path.home() / ".vfleet" / "credential.key"

    @field_validator("app_mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        allowed = {"auto", "demo", "vcenter", "esxi"}
        lowered = value.lower().strip()
        if lowered not in allowed:
            raise ValueError(f"APP_MODE must be one of {sorted(allowed)}")
        return lowered

    @property
    def owner_fields(self) -> List[str]:
        return [part.strip() for part in self.owner_field_names.split(",") if part.strip()]

    @property
    def has_vcenter_creds(self) -> bool:
        return bool(self.vcenter_host and self.vcenter_user and self.vcenter_password)

    @property
    def resolved_mode(self) -> str:
        if self.app_mode == "auto":
            return "vcenter" if self.has_vcenter_creds else "demo"
        return self.app_mode


settings = Settings()
