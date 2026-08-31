from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from .config import Settings
from .models import ConnectionProfileSummary


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectionProfile(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    host: str
    user: str
    password: str
    port: int = 443
    insecure: bool = True
    endpoint_kind: str = "auto"
    endpoint_fingerprint: str = ""
    ssh_enabled: bool = False
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_port: int = 22
    ssh_host_key_sha256: str = ""
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    last_used_at: str = ""

    def summary(self, active_profile_id: str = "") -> ConnectionProfileSummary:
        return ConnectionProfileSummary(
            id=self.id,
            name=self.name,
            host=self.host,
            user=self.user,
            port=self.port,
            insecure=self.insecure,
            endpoint_kind=self.endpoint_kind,
            endpoint_fingerprint=self.endpoint_fingerprint,
            ssh_enabled=self.ssh_enabled,
            ssh_user=self.ssh_user,
            ssh_port=self.ssh_port,
            ssh_host_key_sha256=self.ssh_host_key_sha256,
            has_saved_password=bool(self.password),
            has_saved_ssh_password=bool(self.ssh_password),
            active=self.id == active_profile_id,
            last_used_at=self.last_used_at or None,
        )


class ConnectionProfileStore:
    """Owner-only local persistence for vCenter and standalone ESXi credentials."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

    def _load_unlocked(self) -> List[ConnectionProfile]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = payload.get("profiles", []) if isinstance(payload, dict) else []
            return [ConnectionProfile.model_validate(row) for row in rows]
        except (OSError, ValueError, TypeError):
            return []

    def _save_unlocked(self, profiles: List[ConnectionProfile]) -> None:
        payload = {"version": 1, "profiles": [profile.model_dump() for profile in profiles]}
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def list(self) -> List[ConnectionProfile]:
        with self._lock:
            return self._load_unlocked()

    def get(self, profile_id: str) -> Optional[ConnectionProfile]:
        return next((profile for profile in self.list() if profile.id == profile_id), None)

    def find(self, host: str, user: str, port: int) -> Optional[ConnectionProfile]:
        host_key = host.strip().lower()
        user_key = user.strip().lower()
        return next(
            (
                profile
                for profile in self.list()
                if profile.host.lower() == host_key and profile.user.lower() == user_key and profile.port == port
            ),
            None,
        )

    def save(self, profile: ConnectionProfile) -> ConnectionProfile:
        with self._lock:
            profiles = self._load_unlocked()
            now = _now_iso()
            for index, current in enumerate(profiles):
                if current.id == profile.id:
                    profile.created_at = current.created_at
                    profile.updated_at = now
                    profiles[index] = profile
                    break
            else:
                profile.created_at = now
                profile.updated_at = now
                profiles.append(profile)
            self._save_unlocked(profiles)
        return profile

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            profiles = self._load_unlocked()
            remaining = [profile for profile in profiles if profile.id != profile_id]
            if len(remaining) == len(profiles):
                return False
            self._save_unlocked(remaining)
            return True

    def mark_connected(self, profile_id: str, endpoint_kind: str, endpoint_fingerprint: str) -> None:
        profile = self.get(profile_id)
        if profile is None:
            return
        kind = endpoint_kind or profile.endpoint_kind
        fingerprint = endpoint_fingerprint or profile.endpoint_fingerprint
        if (
            profile.endpoint_kind == kind
            and profile.endpoint_fingerprint == fingerprint
            and profile.last_used_at
        ):
            return
        profile.endpoint_kind = kind
        profile.endpoint_fingerprint = fingerprint
        profile.last_used_at = _now_iso()
        self.save(profile)

    def import_settings(self, settings: Settings) -> str:
        if not settings.has_vcenter_creds:
            return ""
        existing = self.find(settings.vcenter_host, settings.vcenter_user, settings.vcenter_port)
        if existing is not None:
            existing.password = settings.vcenter_password
            existing.insecure = settings.vcenter_insecure
            existing.ssh_enabled = settings.esxi_ssh_enabled
            existing.ssh_user = settings.esxi_ssh_user
            existing.ssh_password = settings.esxi_ssh_password
            existing.ssh_port = settings.esxi_ssh_port
            existing.ssh_host_key_sha256 = settings.esxi_ssh_host_key_sha256
            if settings.app_mode in {"vcenter", "esxi"}:
                existing.endpoint_kind = settings.app_mode
            self.save(existing)
            return existing.id
        kind = settings.app_mode if settings.app_mode in {"vcenter", "esxi"} else "auto"
        profile = ConnectionProfile(
            name=settings.vcenter_host,
            host=settings.vcenter_host,
            user=settings.vcenter_user,
            password=settings.vcenter_password,
            port=settings.vcenter_port,
            insecure=settings.vcenter_insecure,
            endpoint_kind=kind,
            ssh_enabled=settings.esxi_ssh_enabled,
            ssh_user=settings.esxi_ssh_user,
            ssh_password=settings.esxi_ssh_password,
            ssh_port=settings.esxi_ssh_port,
            ssh_host_key_sha256=settings.esxi_ssh_host_key_sha256,
        )
        return self.save(profile).id
