from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from .config import Settings
from .credential_vault import CredentialVault
from .models import ConnectionProfileSummary


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectionProfile(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    host: str
    user: str
    password: str = ""
    credential_id: str = ""
    port: int = 443
    insecure: bool = True
    endpoint_kind: str = "auto"
    endpoint_fingerprint: str = ""
    ssh_enabled: bool = False
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_port: int = 22
    ssh_host_key_sha256: str = ""
    jump_enabled: bool = False
    jump_address: str = ""
    jump_port: int = 22
    jump_host_type: Literal["auto", "windows", "unix"] = "auto"
    jump_credential_id: str = ""
    jump_host_key_sha256: str = ""
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
            jump_enabled=self.jump_enabled,
            jump_address=self.jump_address,
            jump_port=self.jump_port,
            jump_host_type=self.jump_host_type,
            jump_credential_id=self.jump_credential_id,
            jump_host_key_sha256=self.jump_host_key_sha256,
            active=self.id == active_profile_id,
            last_used_at=self.last_used_at or None,
        )


class ConnectionProfileStore:
    """Metadata-only profiles paired with a portable encrypted credential vault."""

    def __init__(self, path: Path, vault: CredentialVault) -> None:
        self.path = path
        self.vault = vault
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

    def _load_unlocked(self) -> tuple[List[ConnectionProfile], str]:
        if not self.path.exists():
            return [], ""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("profile document must be an object")
            version = payload.get("version", 1)
            if version not in {1, 2}:
                raise ValueError(f"unsupported profile document version: {version}")
            rows = payload.get("profiles", [])
            if not isinstance(rows, list):
                raise ValueError("profiles must be a list")
            profiles = [ConnectionProfile.model_validate(row) for row in rows]
            active_profile_id = str(payload.get("active_profile_id", "") or "")
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"Could not read saved connection profiles: {exc}") from exc

        legacy_credentials = {}
        profile_ids = set()
        credential_ids = set()
        for profile in profiles:
            if not profile.id or profile.id in profile_ids:
                raise RuntimeError("Saved connection profile identifiers must be non-empty and unique")
            profile_ids.add(profile.id)
            profile.credential_id = profile.credential_id or profile.id
            if profile.credential_id in credential_ids:
                raise RuntimeError("Saved credential identifiers must be unique per connection profile")
            credential_ids.add(profile.credential_id)
            if profile.password or profile.ssh_password:
                legacy_credentials[profile.credential_id] = {
                    "password": profile.password,
                    "ssh_password": profile.ssh_password,
                }
        if version == 1 or legacy_credentials:
            if legacy_credentials:
                self.vault.put_many(legacy_credentials)
            self._save_unlocked(profiles, active_profile_id)

        if profiles and version >= 2 and not self.vault.path.exists():
            raise RuntimeError("Saved connection profiles exist but the encrypted credential vault is missing")

        for profile in profiles:
            credentials = self.vault.get(profile.credential_id)
            profile.password = credentials["password"]
            profile.ssh_password = credentials["ssh_password"]
        return profiles, active_profile_id

    def _save_unlocked(self, profiles: List[ConnectionProfile], active_profile_id: str) -> None:
        rows = []
        for profile in profiles:
            profile.credential_id = profile.credential_id or profile.id
            rows.append(profile.model_dump(exclude={"password", "ssh_password"}))
        payload = {"version": 2, "active_profile_id": active_profile_id, "profiles": rows}
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def list(self) -> List[ConnectionProfile]:
        with self._lock:
            profiles, _ = self._load_unlocked()
            return profiles

    def active_profile_id(self) -> str:
        with self._lock:
            _, active_profile_id = self._load_unlocked()
            return active_profile_id

    def set_active(self, profile_id: str) -> None:
        with self._lock:
            profiles, _ = self._load_unlocked()
            if profile_id and not any(profile.id == profile_id for profile in profiles):
                raise ValueError("Unknown connection profile")
            self._save_unlocked(profiles, profile_id)

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
            profiles, active_profile_id = self._load_unlocked()
            now = _now_iso()
            profile.credential_id = profile.credential_id or profile.id
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
            self.vault.put(profile.credential_id, profile.password, profile.ssh_password)
            self._save_unlocked(profiles, active_profile_id)
        return profile

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            profiles, active_profile_id = self._load_unlocked()
            removed = next((profile for profile in profiles if profile.id == profile_id), None)
            remaining = [profile for profile in profiles if profile.id != profile_id]
            if removed is None:
                return False
            next_active = "" if active_profile_id == profile_id else active_profile_id
            self._save_unlocked(remaining, next_active)
            self.vault.delete(removed.credential_id or removed.id)
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
