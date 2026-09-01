from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from dotenv import dotenv_values

from .config import ROOT, Settings

def parse_endpoint(raw: str, default_port: int = 443) -> Tuple[str, int]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("vCenter host is required")
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = text.split("/", 1)[0]
    if text.startswith("[") and "]" in text:
        host, rest = text[1:].split("]", 1)
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default_port
    if ":" in text:
        host, port_text = text.rsplit(":", 1)
        if port_text.isdigit():
            if not host:
                raise ValueError("vCenter host is required")
            return host, int(port_text)
    return text, default_port


def quote_env_value(value: str) -> str:
    if value == "" or re.search(r"[\s#\"'\\$]", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def upsert_env(path: Path, updates: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    lines = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                lines.append(f"{key}={quote_env_value(remaining.pop(key))}")
                continue
        lines.append(line)
    if remaining:
        if lines and lines[-1] != "":
            lines.append("")
        for key, value in remaining.items():
            lines.append(f"{key}={quote_env_value(value)}")
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def resolve_login_password(provided: str, host: str, user: str, port: int, settings: Settings) -> str:
    if provided:
        return provided
    if (
        settings.vcenter_password
        and host == settings.vcenter_host
        and user == settings.vcenter_user
        and port == settings.vcenter_port
    ):
        return settings.vcenter_password
    return ""


def resolve_ssh_password(provided: str, host: str, user: str, port: int, settings: Settings) -> str:
    """Reuse an existing SSH secret only for the same saved endpoint and SSH identity."""
    if provided:
        return provided
    if (
        settings.esxi_ssh_password
        and host == settings.vcenter_host
        and user == settings.esxi_ssh_user
        and port == settings.esxi_ssh_port
    ):
        return settings.esxi_ssh_password
    return ""


def persist_vcenter(
    settings: Settings,
    host: str,
    user: str,
    password: str,
    port: int,
    insecure: bool,
    *,
    endpoint_kind: str = "auto",
    ssh_enabled: bool = False,
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_port: int = 22,
    ssh_host_key_sha256: str = "",
) -> None:
    """Apply a saved connection in memory without persisting secrets to .env."""
    settings.app_mode = endpoint_kind if endpoint_kind in {"vcenter", "esxi"} else "auto"
    settings.vcenter_host = host
    settings.vcenter_user = user
    settings.vcenter_password = password
    settings.vcenter_port = port
    settings.vcenter_insecure = insecure
    settings.esxi_ssh_enabled = ssh_enabled
    settings.esxi_ssh_user = ssh_user
    settings.esxi_ssh_password = ssh_password
    settings.esxi_ssh_port = ssh_port
    settings.esxi_ssh_host_key_sha256 = ssh_host_key_sha256


def clear_legacy_env_secrets(
    path: Path = ROOT / ".env",
    *,
    expected_host: Optional[str] = None,
    expected_user: Optional[str] = None,
    expected_port: Optional[int] = None,
    expected_password: Optional[str] = None,
    expected_ssh_password: Optional[str] = None,
) -> None:
    """Remove legacy plaintext secrets after they have been migrated to the vault."""
    if not path.exists():
        return
    updates: Dict[str, str] = {}
    if expected_host is None:
        updates = {"VCENTER_PASSWORD": "", "ESXI_SSH_PASSWORD": ""}
    else:
        values = dotenv_values(path)
        try:
            env_port = int(str(values.get("VCENTER_PORT", "443") or "443"))
        except ValueError:
            env_port = 443
        same_identity = (
            str(values.get("VCENTER_HOST", "") or "").strip().lower() == expected_host.strip().lower()
            and str(values.get("VCENTER_USER", "") or "").strip().lower()
            == str(expected_user or "").strip().lower()
            and env_port == (expected_port or 443)
        )
        if same_identity and str(values.get("VCENTER_PASSWORD", "") or "") == str(expected_password or ""):
            updates["VCENTER_PASSWORD"] = ""
        if same_identity and str(values.get("ESXI_SSH_PASSWORD", "") or "") == str(expected_ssh_password or ""):
            updates["ESXI_SSH_PASSWORD"] = ""
    if updates:
        upsert_env(path, updates)


def forget_vcenter(settings: Settings) -> None:
    upsert_env(
        ROOT / ".env",
        {
            "VCENTER_HOST": "",
            "VCENTER_USER": "",
            "VCENTER_PASSWORD": "",
            "ESXI_SSH_ENABLED": "false",
            "ESXI_SSH_USER": "",
            "ESXI_SSH_PASSWORD": "",
            "ESXI_SSH_HOST_KEY_SHA256": "",
        },
    )
    settings.vcenter_host = ""
    settings.vcenter_user = ""
    settings.vcenter_password = ""
    settings.esxi_ssh_enabled = False
    settings.esxi_ssh_user = ""
    settings.esxi_ssh_password = ""
    settings.esxi_ssh_host_key_sha256 = ""
    settings.access_jump_enabled = False
    settings.access_jump_address = ""
    settings.access_jump_host_type = "auto"
    settings.access_jump_user = ""
    settings.access_jump_password = ""
    settings.access_jump_host_key_sha256 = ""


def apply_runtime(
    settings: Settings,
    host: str,
    user: str,
    password: str,
    port: int,
    insecure: bool,
    *,
    ssh_enabled: bool = False,
    ssh_user: str = "",
    ssh_password: str = "",
    ssh_port: int = 22,
    ssh_host_key_sha256: str = "",
) -> Settings:
    return settings.model_copy(
        update={
            "app_mode": "vcenter",
            "vcenter_host": host,
            "vcenter_user": user,
            "vcenter_password": password,
            "vcenter_port": port,
            "vcenter_insecure": insecure,
            "esxi_ssh_enabled": ssh_enabled,
            "esxi_ssh_user": ssh_user,
            "esxi_ssh_password": ssh_password,
            "esxi_ssh_port": ssh_port,
            "esxi_ssh_host_key_sha256": ssh_host_key_sha256,
            "access_jump_enabled": False,
            "access_jump_address": "",
            "access_jump_port": 22,
            "access_jump_host_type": "auto",
            "access_jump_user": "",
            "access_jump_password": "",
            "access_jump_host_key_sha256": "",
        }
    )
