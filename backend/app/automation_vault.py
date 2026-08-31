from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from .credential_vault import CredentialVault


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutomationCredential(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    kind: str
    username: str = ""
    scope: str = "global"
    endpoint_fingerprint: str = ""
    credential_id: str = ""
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    def secret_id(self) -> str:
        return self.credential_id or f"automation:{self.id}"


class AutomationCredentialStore:
    """Non-secret automation metadata paired with encrypted vault records."""

    def __init__(self, path: Path, vault: CredentialVault) -> None:
        self.path = path
        self.vault = vault
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

    def _load_unlocked(self) -> List[AutomationCredential]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("unsupported credential metadata version")
            rows = payload.get("credentials", [])
            if not isinstance(rows, list):
                raise ValueError("credentials must be a list")
            credentials = [AutomationCredential.model_validate(row) for row in rows]
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"Could not read automation credential metadata: {exc}") from exc
        seen = set()
        for credential in credentials:
            if not credential.id or credential.id in seen:
                raise RuntimeError("Automation credential identifiers must be non-empty and unique")
            seen.add(credential.id)
            credential.credential_id = credential.secret_id()
        return credentials

    def _save_unlocked(self, credentials: List[AutomationCredential]) -> None:
        payload = {
            "version": 1,
            "credentials": [credential.model_dump() for credential in credentials],
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def list(self) -> List[AutomationCredential]:
        with self._lock:
            return self._load_unlocked()

    def get(self, credential_id: str) -> Optional[AutomationCredential]:
        return next((item for item in self.list() if item.id == credential_id), None)

    def secret(self, credential_id: str) -> str:
        credential = self.get(credential_id)
        if credential is None:
            return ""
        return self.vault.get_fields(credential.secret_id()).get("secret", "")

    def save(
        self,
        *,
        credential_id: str = "",
        name: str,
        kind: str,
        username: str,
        secret: str,
        scope: str,
        endpoint_fingerprint: str,
    ) -> AutomationCredential:
        with self._lock:
            credentials = self._load_unlocked()
            existing = next((item for item in credentials if item.id == credential_id), None)
            now = _now_iso()
            if existing is None:
                existing = AutomationCredential(
                    id=credential_id or str(uuid.uuid4()),
                    name=name,
                    kind=kind,
                    username=username,
                    scope=scope,
                    endpoint_fingerprint=endpoint_fingerprint if scope == "endpoint" else "",
                )
                existing.credential_id = existing.secret_id()
                credentials.append(existing)
            else:
                existing.name = name
                existing.kind = kind
                existing.username = username
                existing.scope = scope
                existing.endpoint_fingerprint = endpoint_fingerprint if scope == "endpoint" else ""
                existing.updated_at = now
            if secret:
                self.vault.put_fields(existing.secret_id(), {"secret": secret})
            elif not self.vault.get_fields(existing.secret_id()).get("secret"):
                raise ValueError("A credential secret is required")
            self._save_unlocked(credentials)
            return existing

    def delete(self, credential_id: str) -> bool:
        with self._lock:
            credentials = self._load_unlocked()
            target = next((item for item in credentials if item.id == credential_id), None)
            if target is None:
                return False
            remaining = [item for item in credentials if item.id != credential_id]
            self._save_unlocked(remaining)
            self.vault.delete(target.secret_id())
            return True
