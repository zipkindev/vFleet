import json
from pathlib import Path

from app.config import Settings
from app.credential_vault import CredentialVault
from app.profiles import ConnectionProfile, ConnectionProfileStore
from app.store import LocalStore


def profile_store(tmp_path: Path) -> ConnectionProfileStore:
    vault = CredentialVault(
        tmp_path / "credentials.enc.json",
        key_file=tmp_path / "secrets" / "credential.key",
    )
    return ConnectionProfileStore(tmp_path / "connections.json", vault)


def test_connection_profiles_encrypt_secrets_and_never_return_them_in_summary(tmp_path: Path):
    path = tmp_path / "connections.json"
    store = profile_store(tmp_path)
    saved = store.save(
        ConnectionProfile(
            name="Lab ESXi",
            host="esxi.lab.local",
            user="root",
            password="endpoint-secret",
            endpoint_kind="esxi",
            endpoint_fingerprint="fingerprint-esxi",
            ssh_enabled=True,
            ssh_user="root",
            ssh_password="ssh-secret",
            ssh_host_key_sha256="SHA256:public-key-fingerprint",
            jump_enabled=True,
            jump_address="access.lab.local",
            jump_host_type="windows",
            jump_credential_id="jump-credential",
            jump_host_key_sha256="SHA256:jump-key",
        )
    )

    reloaded = profile_store(tmp_path).get(saved.id)
    assert reloaded is not None
    assert reloaded.password == "endpoint-secret"
    assert reloaded.ssh_password == "ssh-secret"
    summary = reloaded.summary(saved.id).model_dump()
    assert "password" not in summary
    assert "ssh_password" not in summary
    assert summary["has_saved_password"] is True
    assert summary["has_saved_ssh_password"] is True
    assert summary["jump_address"] == "access.lab.local"
    assert summary["jump_host_type"] == "windows"
    assert summary["jump_credential_id"] == "jump-credential"
    assert summary["jump_host_key_sha256"] == "SHA256:jump-key"
    assert summary["active"] is True
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "credentials.enc.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "secrets" / "credential.key").stat().st_mode & 0o777 == 0o600
    metadata = path.read_text(encoding="utf-8")
    encrypted = (tmp_path / "credentials.enc.json").read_text(encoding="utf-8")
    assert "endpoint-secret" not in metadata
    assert "ssh-secret" not in metadata
    assert "endpoint-secret" not in encrypted
    assert "ssh-secret" not in encrypted


def test_import_settings_keeps_multiple_endpoints_and_deduplicates_identity(tmp_path: Path):
    store = profile_store(tmp_path)
    first = Settings(vcenter_host="vc.one", vcenter_user="admin", vcenter_password="one", vcenter_port=443)
    second = Settings(vcenter_host="esxi.two", vcenter_user="root", vcenter_password="two", vcenter_port=443)

    first_id = store.import_settings(first)
    assert store.import_settings(first) == first_id
    second_id = store.import_settings(second)
    assert second_id != first_id
    assert {profile.host for profile in store.list()} == {"vc.one", "esxi.two"}


def test_legacy_plaintext_profile_is_migrated_atomically(tmp_path: Path):
    path = tmp_path / "connections.json"
    legacy = ConnectionProfile(
        id="legacy-profile",
        name="Legacy",
        host="legacy.example",
        user="administrator",
        password="legacy-password",
        ssh_password="legacy-ssh-password",
    )
    path.write_text(json.dumps({"version": 1, "profiles": [legacy.model_dump()]}), encoding="utf-8")

    store = profile_store(tmp_path)
    migrated = store.get("legacy-profile")

    assert migrated is not None
    assert migrated.password == "legacy-password"
    assert migrated.ssh_password == "legacy-ssh-password"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert metadata["version"] == 2
    assert "password" not in metadata["profiles"][0]
    assert "ssh_password" not in metadata["profiles"][0]


def test_active_profile_and_credential_deletion_are_persistent(tmp_path: Path):
    store = profile_store(tmp_path)
    saved = store.save(
        ConnectionProfile(name="Lab", host="lab.example", user="root", password="secret")
    )
    store.set_active(saved.id)
    assert profile_store(tmp_path).active_profile_id() == saved.id

    store.set_active("")
    assert store.delete(saved.id) is True
    assert store.get(saved.id) is None
    vault_payload = json.loads((tmp_path / "credentials.enc.json").read_text(encoding="utf-8"))
    assert vault_payload["records"] == {}


def test_job_history_is_scoped_and_only_terminal_rows_are_cleared(tmp_path: Path):
    store = LocalStore(tmp_path / "vfleet.db")
    try:
        first = store.enqueue("power", "first", {"_endpoint_fingerprint": "endpoint-a"})
        second = store.enqueue("power", "second", {"_endpoint_fingerprint": "endpoint-b"})
        store.complete(first.id)

        assert [job.id for job in store.list_jobs(endpoint_fingerprint="endpoint-a")] == [first.id]
        assert [job.id for job in store.list_jobs(endpoint_fingerprint="endpoint-b")] == [second.id]
        assert store.clear_job_history("endpoint-a") == 1
        assert store.get(first.id) is None
        assert store.get(second.id) is not None
        assert store.clear_job_history("endpoint-b") == 0
        store.complete(second.id)
        third = store.enqueue("power", "third", {"_endpoint_fingerprint": "endpoint-a"})
        store.complete(third.id)
        assert store.clear_other_job_history("endpoint-a") == 1
        assert store.get(third.id) is not None
        assert store.clear_job_history("endpoint-a") == 1
        assert store.job_count() == 0
    finally:
        store.close()
