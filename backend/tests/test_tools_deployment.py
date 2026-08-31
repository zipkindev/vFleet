from pathlib import Path

from fastapi.testclient import TestClient

from app import guest_tools
from app.automation_vault import AutomationCredentialStore
from app.config import Settings
from app.credential_vault import CredentialVault
from app.models import Job
from app.relay import RelayWorker
from app.store import LocalStore
from app.main import app


class ToolsAdapter:
    def __init__(self):
        self.checks = 0
        self.restored = False

    def tools_status(self, vm_id):
        self.checks += 1
        return {"running": self.checks >= 2, "running_status": "guestToolsRunning" if self.checks >= 2 else "guestToolsNotRunning"}

    def mount_tools_installer(self, vm_id):
        return {"media": {"file_name": "[data] prior.iso"}}

    def restore_tools_media(self, vm_id, media):
        self.restored = media.get("file_name") == "[data] prior.iso"


def build_worker(tmp_path: Path, kind: str):
    store = LocalStore(tmp_path / "jobs.db")
    vault = CredentialVault(tmp_path / "secrets.enc.json", key_file=tmp_path / "key")
    credentials = AutomationCredentialStore(tmp_path / "automation.json", vault)
    credential = credentials.save(name="guest admin", kind=kind, username="admin", secret="secret", scope="global", endpoint_fingerprint="")
    adapter = ToolsAdapter()
    worker = RelayWorker(Settings(data_dir=tmp_path), store, lambda: adapter, credentials)
    return worker, store, adapter, credential


def enqueue(store, credential, family):
    return store.enqueue("tools_deploy", "Deploy tools", {
        "_endpoint_fingerprint": "demo",
        "vm_id": "vm-1",
        "address": "10.0.0.10",
        "os_family": family,
        "credential_id": credential.id,
        "windows_transport": "http",
        "windows_port": 5985,
        "validate_certificate": True,
        "linux_port": 22,
        "sudo": True,
        "ssh_host_key_sha256": "SHA256:test",
    })


def test_windows_tools_job_mounts_installs_verifies_and_restores(tmp_path, monkeypatch):
    worker, store, adapter, credential = build_worker(tmp_path, "windows")
    monkeypatch.setattr(guest_tools, "windows_preflight", lambda *args, **kwargs: {"administrator": True})
    monkeypatch.setattr(guest_tools, "windows_install", lambda *args, **kwargs: {"reboot_scheduled": True})
    job = enqueue(store, credential, "windows")

    result = worker._deploy_guest_tools(job, adapter, {})

    assert result["tools"]["running"] is True
    assert adapter.restored is True
    assert "secret" not in str(job.payload)


def test_linux_tools_job_uses_ssh_without_mounting_media(tmp_path, monkeypatch):
    worker, store, adapter, credential = build_worker(tmp_path, "ssh")
    monkeypatch.setattr(guest_tools, "linux_preflight", lambda *args, **kwargs: {"id": "ubuntu"})
    monkeypatch.setattr(guest_tools, "linux_install", lambda *args, **kwargs: {"message": "installed"})
    job = enqueue(store, credential, "linux")
    adapter.mount_tools_installer = lambda vm_id: (_ for _ in ()).throw(AssertionError("Linux must not mount an ISO"))

    result = worker._deploy_guest_tools(job, adapter, {})

    assert result["os_family"] == "linux"
    assert result["tools"]["running"] is True


def test_tools_api_queues_secret_free_job_and_vault_never_returns_secret():
    with TestClient(app) as client:
        created = client.post("/api/automation-credentials", json={
            "name": "API Windows admin",
            "kind": "windows",
            "username": "Administrator",
            "secret": "must-never-appear-in-job",
            "scope": "global",
            "confirm": True,
        })
        assert created.status_code == 200
        credential_id = created.json()["id"]
        assert "secret" not in created.json()
        listed = client.get("/api/automation-credentials")
        assert "must-never-appear-in-job" not in listed.text

        queued = client.post("/api/tools/deploy", json={
            "targets": [{"vm_id": "vm-702", "address": "127.0.0.1", "os_family": "windows"}],
            "credential_id": credential_id,
            "windows_transport": "http",
            "windows_port": 5985,
            "validate_certificate": True,
            "confirm": True,
        })
        assert queued.status_code == 200, queued.text
        assert len(queued.json()["jobs"]) == 1
        assert "must-never-appear-in-job" not in queued.text
        assert queued.json()["jobs"][0]["payload"]["credential_id"] == credential_id
