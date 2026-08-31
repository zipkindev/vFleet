from __future__ import annotations

import base64
import binascii
import json
import os
import threading
from pathlib import Path
from typing import Dict, Mapping, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


VAULT_VERSION = 1
VAULT_CIPHER = "AES-256-GCM"
_AAD_PREFIX = b"vfleet:connection-profile:v1:"


class CredentialVaultError(RuntimeError):
    """Raised when the encrypted credential vault cannot be used safely."""


class CredentialVault:
    """Portable encrypted-JSON credential storage backed by a separate key."""

    def __init__(self, path: Path, *, key_file: Path, master_key: str = "") -> None:
        self.path = path
        self.key_file = key_file
        self._configured_key = master_key.strip()
        self._cached_key: Optional[bytes] = None
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secure_directory(self.path.parent)

    @staticmethod
    def default_key_file() -> Path:
        return Path.home() / ".vfleet" / "credential.key"

    @property
    def storage_label(self) -> str:
        return "Encrypted JSON vault (AES-256-GCM)"

    @staticmethod
    def _secure_directory(path: Path) -> None:
        try:
            path.chmod(0o700)
        except OSError:
            pass

    @staticmethod
    def _decode_key(encoded: str) -> bytes:
        try:
            key = base64.b64decode(encoded.strip(), altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CredentialVaultError("The configured vFleet master key is not valid base64") from exc
        if len(key) != 32:
            raise CredentialVaultError("The configured vFleet master key must decode to exactly 32 bytes")
        return key

    def _create_key_file(self) -> bytes:
        parent = self.key_file.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            self._secure_directory(parent)
        key = AESGCM.generate_key(bit_length=256)
        encoded = base64.urlsafe_b64encode(key).decode("ascii")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(self.key_file, flags, 0o600)
        except FileExistsError:
            return self._read_key_file()
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                self.key_file.unlink()
            except OSError:
                pass
            raise
        return key

    def _read_key_file(self) -> bytes:
        try:
            if self.key_file.is_symlink():
                raise CredentialVaultError(f"Credential key path must not be a symbolic link: {self.key_file}")
            if not self.key_file.is_file():
                raise CredentialVaultError(f"Credential key path is not a file: {self.key_file}")
            encoded = self.key_file.read_text(encoding="ascii")
            try:
                self.key_file.chmod(0o600)
            except OSError:
                pass
        except CredentialVaultError:
            raise
        except OSError as exc:
            raise CredentialVaultError(f"Could not read the vFleet credential key: {exc}") from exc
        return self._decode_key(encoded)

    def _key(self, *, create: bool) -> bytes:
        if self._cached_key is not None:
            return self._cached_key
        if self._configured_key:
            key = self._decode_key(self._configured_key)
        elif self.key_file.exists():
            key = self._read_key_file()
        elif create:
            key = self._create_key_file()
        else:
            raise CredentialVaultError(
                f"The credential vault exists but its master key is missing: {self.key_file}"
            )
        self._cached_key = key
        return key

    def _load_records_unlocked(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise CredentialVaultError(f"Could not read the encrypted credential vault: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != VAULT_VERSION:
            raise CredentialVaultError("Unsupported or malformed credential vault format")
        if payload.get("cipher") != VAULT_CIPHER or not isinstance(payload.get("records"), dict):
            raise CredentialVaultError("Unsupported or malformed credential vault encryption metadata")
        return dict(payload["records"])

    def _save_records_unlocked(self, records: Mapping[str, dict]) -> None:
        payload = {"version": VAULT_VERSION, "cipher": VAULT_CIPHER, "records": dict(records)}
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
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

    @staticmethod
    def _aad(credential_id: str) -> bytes:
        return _AAD_PREFIX + credential_id.encode("utf-8")

    def _encrypt_fields(self, credential_id: str, values: Mapping[str, str]) -> dict:
        normalized = {str(key): str(value) for key, value in values.items()}
        plaintext = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key(create=True)).encrypt(nonce, plaintext, self._aad(credential_id))
        return {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def _decrypt_fields(self, credential_id: str, record: object) -> Dict[str, str]:
        if not isinstance(record, dict):
            raise CredentialVaultError(f"Malformed credential record for profile {credential_id}")
        try:
            nonce = base64.b64decode(str(record["nonce"]), validate=True)
            ciphertext = base64.b64decode(str(record["ciphertext"]), validate=True)
            plaintext = AESGCM(self._key(create=False)).decrypt(
                nonce, ciphertext, self._aad(credential_id)
            )
            payload = json.loads(plaintext.decode("utf-8"))
            if not isinstance(payload, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
            ):
                raise ValueError("credential values must be strings")
            return dict(payload)
        except (InvalidTag, KeyError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialVaultError(
                f"Credential record {credential_id} could not be authenticated or decrypted"
            ) from exc

    def get(self, credential_id: str) -> Dict[str, str]:
        fields = self.get_fields(credential_id)
        return {"password": fields.get("password", ""), "ssh_password": fields.get("ssh_password", "")}

    def get_fields(self, credential_id: str) -> Dict[str, str]:
        with self._lock:
            record = self._load_records_unlocked().get(credential_id)
            if record is None:
                return {}
            return self._decrypt_fields(credential_id, record)

    def put(self, credential_id: str, password: str, ssh_password: str = "") -> None:
        self.put_many({credential_id: {"password": password, "ssh_password": ssh_password}})

    def put_fields(self, credential_id: str, values: Mapping[str, str]) -> None:
        with self._lock:
            records = self._load_records_unlocked()
            for existing_id, existing_record in records.items():
                self._decrypt_fields(existing_id, existing_record)
            record = self._encrypt_fields(credential_id, values)
            self._decrypt_fields(credential_id, record)
            records[credential_id] = record
            self._save_records_unlocked(records)

    def put_many(self, credentials: Mapping[str, Mapping[str, str]]) -> None:
        if not credentials:
            return
        with self._lock:
            records = self._load_records_unlocked()
            for existing_id, existing_record in records.items():
                self._decrypt_fields(existing_id, existing_record)
            for credential_id, values in credentials.items():
                record = self._encrypt_fields(
                    credential_id,
                    {
                        "password": str(values.get("password", "")),
                        "ssh_password": str(values.get("ssh_password", "")),
                    },
                )
                self._decrypt_fields(credential_id, record)
                records[credential_id] = record
            self._save_records_unlocked(records)

    def delete(self, credential_id: str) -> bool:
        with self._lock:
            records = self._load_records_unlocked()
            if credential_id not in records:
                return False
            for existing_id, existing_record in records.items():
                self._decrypt_fields(existing_id, existing_record)
            del records[credential_id]
            self._save_records_unlocked(records)
            return True
