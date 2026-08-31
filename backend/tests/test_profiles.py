from pathlib import Path

from app.config import Settings
from app.profiles import ConnectionProfile, ConnectionProfileStore
from app.store import LocalStore


def test_connection_profiles_persist_secrets_but_never_return_them_in_summary(tmp_path: Path):
    path = tmp_path / "connections.json"
    store = ConnectionProfileStore(path)
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
        )
    )

    reloaded = ConnectionProfileStore(path).get(saved.id)
    assert reloaded is not None
    assert reloaded.password == "endpoint-secret"
    assert reloaded.ssh_password == "ssh-secret"
    summary = reloaded.summary(saved.id).model_dump()
    assert "password" not in summary
    assert "ssh_password" not in summary
    assert summary["has_saved_password"] is True
    assert summary["has_saved_ssh_password"] is True
    assert summary["active"] is True
    assert path.stat().st_mode & 0o777 == 0o600


def test_import_settings_keeps_multiple_endpoints_and_deduplicates_identity(tmp_path: Path):
    store = ConnectionProfileStore(tmp_path / "connections.json")
    first = Settings(vcenter_host="vc.one", vcenter_user="admin", vcenter_password="one", vcenter_port=443)
    second = Settings(vcenter_host="esxi.two", vcenter_user="root", vcenter_password="two", vcenter_port=443)

    first_id = store.import_settings(first)
    assert store.import_settings(first) == first_id
    second_id = store.import_settings(second)
    assert second_id != first_id
    assert {profile.host for profile in store.list()} == {"vc.one", "esxi.two"}


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
