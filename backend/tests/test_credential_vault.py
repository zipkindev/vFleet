import base64
import json
import os
from pathlib import Path

import pytest

from app.credential_vault import CredentialVault, CredentialVaultError


def build_vault(tmp_path: Path, *, master_key: str = "") -> CredentialVault:
    return CredentialVault(
        tmp_path / "credentials.enc.json",
        key_file=tmp_path / "key-store" / "credential.key",
        master_key=master_key,
    )


def test_vault_encrypts_round_trips_and_binds_record_identity(tmp_path: Path):
    vault = build_vault(tmp_path)
    vault.put("profile-a", "api-secret", "ssh-secret")

    assert build_vault(tmp_path).get("profile-a") == {
        "password": "api-secret",
        "ssh_password": "ssh-secret",
    }
    raw = (tmp_path / "credentials.enc.json").read_text(encoding="utf-8")
    assert "api-secret" not in raw
    assert "ssh-secret" not in raw

    payload = json.loads(raw)
    payload["records"]["profile-b"] = payload["records"].pop("profile-a")
    (tmp_path / "credentials.enc.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CredentialVaultError, match="authenticated or decrypted"):
        build_vault(tmp_path).get("profile-b")


def test_vault_rejects_wrong_or_missing_master_key(tmp_path: Path):
    vault = build_vault(tmp_path)
    vault.put("profile-a", "secret")

    wrong_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    with pytest.raises(CredentialVaultError, match="authenticated or decrypted"):
        build_vault(tmp_path, master_key=wrong_key).get("profile-a")
    with pytest.raises(CredentialVaultError, match="authenticated or decrypted"):
        build_vault(tmp_path, master_key=wrong_key).put("profile-b", "another-secret")

    original = build_vault(tmp_path).get("profile-a")
    assert original["password"] == "secret"

    (tmp_path / "key-store" / "credential.key").unlink()
    with pytest.raises(CredentialVaultError, match="master key is missing"):
        build_vault(tmp_path).get("profile-a")


def test_vault_rejects_invalid_configured_key(tmp_path: Path):
    with pytest.raises(CredentialVaultError, match="valid base64"):
        build_vault(tmp_path, master_key="not-base64!!").put("profile", "secret")


def test_vault_rejects_symbolic_link_key_file(tmp_path: Path):
    target = tmp_path / "actual.key"
    target.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"), encoding="ascii")
    key_dir = tmp_path / "key-store"
    key_dir.mkdir()
    (key_dir / "credential.key").symlink_to(target)

    with pytest.raises(CredentialVaultError, match="symbolic link"):
        build_vault(tmp_path).put("profile", "secret")
