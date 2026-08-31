import time

from fastapi.testclient import TestClient

from app.cli import parser
from app.errors import PermanentError
from app.esxi_ssh import datastore_path
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
