from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
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

    name_group_pattern: str = r"^([A-Za-z][A-Za-z0-9]+)"
    owner_field_names: str = "Owner,owner,User,user,CreatedBy,createdBy"

    idle_days: int = 14
    cpu_idle_pct: float = 8.0
    memory_idle_pct: float = 20.0
    event_lookback_days: int = 30

    cache_ttl_seconds: int = Field(default=20, ge=5, le=300)
    data_dir: Path = Field(default=ROOT / "data")
    relay_tick_seconds: float = Field(default=1.0, ge=0.2, le=30)
    sync_interval_seconds: int = Field(default=30, ge=10, le=600)
    connect_timeout_seconds: int = Field(default=20, ge=5, le=120)
    upload_chunk_bytes: int = Field(default=4 * 1024 * 1024, ge=256 * 1024)

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
