from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Tuple

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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
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


def persist_vcenter(settings: Settings, host: str, user: str, password: str, port: int, insecure: bool) -> None:
    upsert_env(
        ROOT / ".env",
        {
            "APP_MODE": "auto",
            "VCENTER_HOST": host,
            "VCENTER_USER": user,
            "VCENTER_PASSWORD": password,
            "VCENTER_PORT": str(port),
            "VCENTER_INSECURE": "true" if insecure else "false",
        },
    )
    settings.app_mode = "auto"
    settings.vcenter_host = host
    settings.vcenter_user = user
    settings.vcenter_password = password
    settings.vcenter_port = port
    settings.vcenter_insecure = insecure


def forget_vcenter(settings: Settings) -> None:
    upsert_env(ROOT / ".env", {"VCENTER_HOST": "", "VCENTER_USER": "", "VCENTER_PASSWORD": ""})
    settings.vcenter_host = ""
    settings.vcenter_user = ""
    settings.vcenter_password = ""


def apply_runtime(settings: Settings, host: str, user: str, password: str, port: int, insecure: bool) -> Settings:
    return settings.model_copy(
        update={
            "app_mode": "vcenter",
            "vcenter_host": host,
            "vcenter_user": user,
            "vcenter_password": password,
            "vcenter_port": port,
            "vcenter_insecure": insecure,
        }
    )
