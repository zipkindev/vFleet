import time
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
from app.profiles import ConnectionProfile


def _wait_job(client: TestClient, job_id: str) -> dict:
    job = None
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert job is not None
    return job


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


def build_worker(tmp_path: Path, kind: str, username: str = "admin"):
    store = LocalStore(tmp_path / "jobs.db")
    vault = CredentialVault(tmp_path / "secrets.enc.json", key_file=tmp_path / "key")
    credentials = AutomationCredentialStore(tmp_path / "automation.json", vault)
    credential = credentials.save(name="guest admin", kind=kind, username=username, secret="secret", scope="global", endpoint_fingerprint="")
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


def test_pfsense_tools_job_uses_root_ssh_through_pinned_jump(tmp_path, monkeypatch):
    worker, store, adapter, credential = build_worker(tmp_path, "ssh", username="root")
    jump_credential = worker.automation_credentials.save(
        name="jump host",
        kind="ssh",
        username="jump.operator",
        secret="jump-secret",
        scope="global",
        endpoint_fingerprint="",
    )
    captured = {}

    def preflight(*args, **kwargs):
        captured["preflight_jump"] = kwargs["jump"]
        return {"platform": "pfSense", "uid": 0}

    def install(*args, **kwargs):
        captured["install_jump"] = kwargs["jump"]
        return {"message": "installed"}

    monkeypatch.setattr(guest_tools, "pfsense_preflight", preflight)
    monkeypatch.setattr(guest_tools, "pfsense_install", install)
    job = enqueue(store, credential, "pfsense")
    job.payload.update({
        "address": "10.0.0.1",
        "jump_address": "192.168.1.60",
        "jump_port": 22,
        "jump_credential_id": jump_credential.id,
        "jump_host_key_sha256": "SHA256:jump",
    })
    adapter.mount_tools_installer = lambda vm_id: (_ for _ in ()).throw(AssertionError("pfSense must not mount an ISO"))

    result = worker._deploy_guest_tools(job, adapter, {})

    assert result["os_family"] == "pfsense"
    assert captured["preflight_jump"]["username"] == "jump.operator"
    assert captured["install_jump"]["host_key_sha256"] == "SHA256:jump"
    assert "secret" not in str(job.payload)
    assert "jump-secret" not in str(job.payload)


def test_tools_api_queues_secret_free_job_and_vault_never_returns_secret(monkeypatch):
    monkeypatch.setattr(
        RelayWorker,
        "_deploy_guest_tools",
        lambda self, job, adapter, progress: {"test": "completed without remote access"},
    )
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
        assert _wait_job(client, queued.json()["jobs"][0]["id"])["status"] == "succeeded"


def test_ssh_host_key_review_uses_vaulted_pinned_jump_credential(monkeypatch):
    captured = {}

    def read_key(address, port, *, jump=None):
        captured.update({"address": address, "port": port, "jump": jump})
        return "SHA256:target"

    monkeypatch.setattr(guest_tools, "ssh_host_key_sha256", read_key)
    with TestClient(app) as client:
        created = client.post("/api/automation-credentials", json={
            "name": "API jump host",
            "kind": "ssh",
            "username": "jump-user",
            "secret": "jump-password-must-stay-secret",
            "scope": "global",
            "confirm": True,
        })
        assert created.status_code == 200

        reviewed = client.get("/api/tools/ssh-host-key", params={
            "address": "10.0.0.1",
            "port": 22,
            "jump_address": "192.168.1.60",
            "jump_port": 22,
            "jump_credential_id": created.json()["id"],
            "jump_host_key_sha256": "SHA256:jump",
        })

        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["fingerprint"] == "SHA256:target"
        assert captured["jump"]["username"] == "jump-user"
        assert captured["jump"]["host_key_sha256"] == "SHA256:jump"
        assert "jump-password-must-stay-secret" not in reviewed.text


def test_jump_host_connection_test_uses_vault_secret_and_pinned_key(monkeypatch):
    captured = {}

    def test_connection(address, username, password, **kwargs):
        captured.update({"address": address, "username": username, "password": password, **kwargs})
        return "windows"

    monkeypatch.setattr(guest_tools, "ssh_connection_test", test_connection)
    with TestClient(app) as client:
        created = client.post("/api/automation-credentials", json={
            "name": "Connection profile jump",
            "kind": "ssh",
            "username": "access-user",
            "secret": "profile-jump-secret",
            "scope": "global",
            "confirm": True,
        })
        tested = client.post("/api/tools/ssh-connection-test", json={
            "address": "192.168.1.60",
            "port": 22,
            "credential_id": created.json()["id"],
            "host_key_sha256": "SHA256:pinned-jump",
            "host_type": "auto",
        })

        assert tested.status_code == 200, tested.text
        assert captured["username"] == "access-user"
        assert captured["password"] == "profile-jump-secret"
        assert captured["host_key_sha256"] == "SHA256:pinned-jump"
        assert captured["host_type"] == "auto"
        assert tested.json()["detected_host_type"] == "windows"
        assert "profile-jump-secret" not in tested.text


def test_vault_credential_cannot_be_deleted_while_connection_profile_references_it():
    with TestClient(app) as client:
        created = client.post("/api/automation-credentials", json={
            "name": "Referenced profile jump",
            "kind": "ssh",
            "username": "access-user",
            "secret": "profile-jump-secret",
            "scope": "global",
            "confirm": True,
        })
        credential_id = created.json()["id"]
        profile = app.state.profile_store.save(ConnectionProfile(
            name="Profile with jump",
            host="vc.example",
            user="administrator",
            password="endpoint-secret",
            endpoint_fingerprint="profile-fingerprint",
            jump_enabled=True,
            jump_address="access.example",
            jump_credential_id=credential_id,
            jump_host_key_sha256="SHA256:jump",
        ))
        try:
            deleted = client.request(
                "DELETE",
                f"/api/automation-credentials/{credential_id}",
                json={"confirm": True},
            )
            assert deleted.status_code == 409
            assert "saved connection profile" in deleted.json()["detail"]
        finally:
            app.state.profile_store.delete(profile.id)


def test_connection_editor_lists_credentials_scoped_to_that_saved_profile():
    with TestClient(app) as client:
        credential = app.state.automation_credentials.save(
            name="Isolated access host",
            kind="ssh",
            username="access-user",
            secret="isolated-secret",
            scope="endpoint",
            endpoint_fingerprint="isolated-endpoint",
        )
        profile = app.state.profile_store.save(ConnectionProfile(
            name="Isolated vCenter",
            host="isolated-vc.example",
            user="administrator",
            password="endpoint-secret",
            endpoint_fingerprint="isolated-endpoint",
        ))
        try:
            current = client.get("/api/automation-credentials").json()["credentials"]
            scoped = client.get(
                "/api/automation-credentials",
                params={"profile_id": profile.id},
            ).json()["credentials"]
            assert credential.id not in {item["id"] for item in current}
            assert credential.id in {item["id"] for item in scoped}
            assert "isolated-secret" not in str(scoped)
        finally:
            app.state.profile_store.delete(profile.id)
            app.state.automation_credentials.delete(credential.id)


def test_pfsense_api_queues_root_target_and_jump_credentials_without_secrets(monkeypatch):
    monkeypatch.setattr(
        RelayWorker,
        "_deploy_guest_tools",
        lambda self, job, adapter, progress: {"test": "completed without remote access"},
    )
    with TestClient(app) as client:
        target = client.post("/api/automation-credentials", json={
            "name": "API pfSense root",
            "kind": "ssh",
            "username": "root",
            "secret": "target-password-must-stay-secret",
            "scope": "global",
            "confirm": True,
        })
        jump = client.post("/api/automation-credentials", json={
            "name": "API pfSense jump",
            "kind": "ssh",
            "username": "jump-user",
            "secret": "jump-password-must-stay-secret",
            "scope": "global",
            "confirm": True,
        })
        assert target.status_code == 200
        assert jump.status_code == 200

        queued = client.post("/api/tools/deploy", json={
            "targets": [{
                "vm_id": "vm-702",
                "address": "10.0.0.1",
                "os_family": "pfsense",
                "ssh_host_key_sha256": "SHA256:target",
            }],
            "credential_id": target.json()["id"],
            "linux_port": 22,
            "jump_address": "192.168.1.60",
            "jump_port": 22,
            "jump_credential_id": jump.json()["id"],
            "jump_host_key_sha256": "SHA256:jump",
            "confirm": True,
        })

        assert queued.status_code == 200, queued.text
        payload = queued.json()["jobs"][0]["payload"]
        assert payload["os_family"] == "pfsense"
        assert payload["jump_credential_id"] == jump.json()["id"]
        assert "target-password-must-stay-secret" not in queued.text
        assert "jump-password-must-stay-secret" not in queued.text
        assert _wait_job(client, queued.json()["jobs"][0]["id"])["status"] == "succeeded"


def test_pfsense_dry_run_preflight_does_not_queue_install(monkeypatch):
    captured = {}

    def preflight(address, username, password, **kwargs):
        captured.update({"address": address, "username": username, "password": password, **kwargs})
        return {"platform": "pfSense", "uid": 0, "installed": False}

    monkeypatch.setattr(guest_tools, "pfsense_preflight", preflight)
    with TestClient(app) as client:
        target = client.post("/api/automation-credentials", json={
            "name": "Dry-run pfSense root",
            "kind": "ssh",
            "username": "root",
            "secret": "dry-run-target-secret",
            "scope": "global",
            "confirm": True,
        })
        jump = client.post("/api/automation-credentials", json={
            "name": "Dry-run jump",
            "kind": "ssh",
            "username": "jump-user",
            "secret": "dry-run-jump-secret",
            "scope": "global",
            "confirm": True,
        })
        before = len(client.get("/api/jobs").json()["jobs"])
        checked = client.post("/api/tools/preflight", json={
            "targets": [{
                "vm_id": "vm-702",
                "address": "10.0.0.1",
                "os_family": "pfsense",
                "ssh_host_key_sha256": "SHA256:target",
            }],
            "credential_id": target.json()["id"],
            "linux_port": 22,
            "jump_address": "192.168.1.60",
            "jump_port": 22,
            "jump_credential_id": jump.json()["id"],
            "jump_host_key_sha256": "SHA256:jump",
        })

        assert checked.status_code == 200, checked.text
        assert checked.json()["details"]["platform"] == "pfSense"
        assert captured["username"] == "root"
        assert captured["jump"]["username"] == "jump-user"
        assert len(client.get("/api/jobs").json()["jobs"]) == before
        assert "dry-run-target-secret" not in checked.text
        assert "dry-run-jump-secret" not in checked.text
