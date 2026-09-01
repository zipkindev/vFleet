import time

from fastapi.testclient import TestClient

from app.cli import parser
from app.errors import PermanentError
from app.esxi_ssh import EsxiSshExecutor, datastore_path
from app.config import Settings
from app.main import app


def _wait(client: TestClient, job_id: str) -> dict:
    for _ in range(120):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.03)
    return job


def test_disk_conversion_is_planned_confirmed_and_endpoint_bound():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        vm = next(item for item in inventory["vms"] if item["disk_provisioning"] == "thick")
        reviewed = client.post(
            "/api/vms/disk-conversion/plan",
            json={"vm_id": vm["id"], "target": "thin", "method": "auto"},
        )
        assert reviewed.status_code == 200
        plan = reviewed.json()
        assert plan["disks"]
        assert plan["can_execute"] is True
        stale = client.post(
            "/api/vms/disk-conversion",
            json={"vm_id": vm["id"], "target": "thin", "method": "auto", "plan_token": "wrong", "confirm": True},
        )
        assert stale.status_code == 409
        queued = client.post(
            "/api/vms/disk-conversion",
            json={"vm_id": vm["id"], "target": "thin", "method": "auto", "plan_token": plan["plan_token"], "confirm": True},
        )
        assert queued.status_code == 200
        job = queued.json()
        assert job["payload"]["_endpoint_fingerprint"] == "demo"
        finished = _wait(client, job["id"])
        assert finished["status"] == "succeeded", finished


def test_host_admin_contract_and_queued_service_action():
    with TestClient(app) as client:
        response = client.get("/api/host")
        assert response.status_code == 200
        host = response.json()
        assert host["services"]
        assert host["storage_adapters"]
        denied = client.post(
            "/api/host/services",
            json={"service_key": "TSM-SSH", "action": "start", "confirm": False},
        )
        assert denied.status_code == 400
        queued = client.post(
            "/api/host/services",
            json={"service_key": "TSM-SSH", "action": "start", "confirm": True},
        )
        assert queued.status_code == 200
        finished = _wait(client, queued.json()["id"])
        assert finished["status"] == "succeeded", finished


def test_cli_exposes_direct_esxi_workflows():
    root = parser()
    assert root.parse_args(["host"]).command == "host"
    parsed = root.parse_args(["disk-convert", "vm-1", "--target", "thin", "--yes"])
    assert parsed.command == "disk-convert"
    assert parsed.yes is True
    tools = root.parse_args(["tools-deploy", "credential", "vm-1=10.0.0.1", "--jump-host-type", "windows", "--no-jump", "--yes"])
    assert tools.no_jump is True
    assert tools.jump_host_type == "windows"


def test_ssh_datastore_paths_are_canonical_and_reject_traversal():
    datastore, relative, full = datastore_path("[datastore1] lab/vm.vmdk")
    assert datastore == "datastore1"
    assert relative == "lab/vm.vmdk"
    assert full == "/vmfs/volumes/datastore1/lab/vm.vmdk"
    try:
        datastore_path("[datastore1] lab/../escape.vmdk")
    except PermanentError:
        pass
    else:
        raise AssertionError("path traversal must be rejected")


def test_esxi_ssh_uses_saved_jump_access_path(monkeypatch):
    import paramiko
    from app import guest_tools

    class FakeClient:
        def __init__(self):
            self.connect_args = None

        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **kwargs):
            self.connect_args = kwargs

        def close(self):
            pass

    target_client = FakeClient()
    jump_client = FakeClient()
    channel = object()
    captured = {}

    def open_jump(address, port, jump, timeout):
        captured.update({"address": address, "port": port, "jump": jump, "timeout": timeout})
        return jump_client, channel

    monkeypatch.setattr(paramiko, "SSHClient", lambda: target_client)
    monkeypatch.setattr(guest_tools, "_open_jump_channel", open_jump)
    settings = Settings(
        vcenter_host="esxi.internal",
        vcenter_user="root",
        esxi_ssh_user="root",
        esxi_ssh_password="esxi-secret",
        esxi_ssh_host_key_sha256="SHA256:target",
        access_jump_enabled=True,
        access_jump_address="access.example",
        access_jump_user="jump-user",
        access_jump_password="jump-secret",
        access_jump_host_key_sha256="SHA256:jump",
    )

    connected, opened_jump = EsxiSshExecutor(settings)._connect()

    assert connected is target_client
    assert opened_jump is jump_client
    assert captured["address"] == "esxi.internal"
    assert captured["jump"]["username"] == "jump-user"
    assert target_client.connect_args["sock"] is channel
