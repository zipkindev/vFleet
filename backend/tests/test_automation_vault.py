import json
from pathlib import Path

from app.automation_vault import AutomationCredentialStore
from app.credential_vault import CredentialVault


def build_store(tmp_path: Path) -> AutomationCredentialStore:
    vault = CredentialVault(tmp_path / "secrets.enc.json", key_file=tmp_path / "vault.key")
    return AutomationCredentialStore(tmp_path / "automation.json", vault)


def test_automation_vault_encrypts_secret_and_keeps_metadata_portable(tmp_path: Path):
    store = build_store(tmp_path)
    saved = store.save(
        name="Linux admins",
        kind="ssh",
        username="operator",
        secret="correct horse battery staple",
        scope="endpoint",
        endpoint_fingerprint="esxi:lab",
    )

    assert store.secret(saved.id) == "correct horse battery staple"
    metadata = (tmp_path / "automation.json").read_text(encoding="utf-8")
    encrypted = (tmp_path / "secrets.enc.json").read_text(encoding="utf-8")
    assert "correct horse battery staple" not in metadata
    assert "correct horse battery staple" not in encrypted
    assert json.loads(metadata)["credentials"][0]["username"] == "operator"

    updated = store.save(
        credential_id=saved.id,
        name="Linux administrators",
        kind="ssh",
        username="operator",
        secret="",
        scope="global",
        endpoint_fingerprint="ignored",
    )
    assert updated.endpoint_fingerprint == ""
    assert store.secret(saved.id) == "correct horse battery staple"
    assert store.delete(saved.id) is True
    assert store.secret(saved.id) == ""


def test_generic_vault_fields_remain_compatible_with_connection_records(tmp_path: Path):
    vault = CredentialVault(tmp_path / "secrets.enc.json", key_file=tmp_path / "vault.key")
    vault.put("connection", "api-password", "ssh-password")
    vault.put_fields("automation", {"secret": "service-token"})

    assert vault.get("connection") == {"password": "api-password", "ssh_password": "ssh-password"}
    assert vault.get_fields("automation") == {"secret": "service-token"}
