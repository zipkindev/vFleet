import time

from fastapi.testclient import TestClient

from app.main import app
from app.vm_storage import apply_disk_transform, normalize_disk_transform


def _wait_job(client: TestClient, job_id: str) -> dict:
    job = None
    for _ in range(80):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert job is not None
    return job


def test_normalize_disk_transform():
    assert normalize_disk_transform("") == ""
    assert normalize_disk_transform("keep") == ""
    assert normalize_disk_transform("Thin") == "thin"
    used, kind = apply_disk_transform(1000, "thin")
    assert kind == "thin"
    assert used == 220


def test_migrate_requires_confirm_and_a_change():
    with TestClient(app) as client:
        missing = client.post("/api/vms/migrate", json={"vm_ids": ["vm-104"], "confirm": True})
        assert missing.status_code == 400
        no_confirm = client.post(
            "/api/vms/migrate",
            json={"vm_ids": ["vm-104"], "host_id": "host-esx03", "confirm": False},
        )
        assert no_confirm.status_code == 400


def test_migrate_rejects_cross_cluster():
    with TestClient(app) as client:
        assert client.get("/api/inventory").status_code == 200
        response = client.post(
            "/api/vms/migrate",
            json={"vm_ids": ["vm-104", "vm-301"], "host_id": "host-esx03", "confirm": True},
        )
        assert response.status_code == 400
        assert "cluster" in response.json()["detail"].lower()


def test_compute_migrate_and_thick_to_thin():
    with TestClient(app) as client:
        before = client.get("/api/inventory").json()
        target = next(vm for vm in before["vms"] if vm["id"] == "vm-101")
        assert target["host_id"] == "host-esx01"
        assert target["disk_provisioning"] == "thick"

        moved = client.post(
            "/api/vms/migrate",
            json={"vm_ids": ["vm-101"], "host_id": "host-esx03", "confirm": True},
        )
        assert moved.status_code == 200
        job = _wait_job(client, moved.json()["id"])
        assert job["status"] == "succeeded", job
        after = client.get("/api/inventory").json()
        vm = next(item for item in after["vms"] if item["id"] == "vm-101")
        assert vm["host_id"] == "host-esx03"
        assert vm["host_name"] == "esx03.lab.local"

        convert = client.post(
            "/api/vms/migrate",
            json={"vm_ids": ["vm-101"], "disk_provisioning": "thin", "confirm": True},
        )
        assert convert.status_code == 200
        job = _wait_job(client, convert.json()["id"])
        assert job["status"] == "succeeded", job
        converted = next(item for item in client.get("/api/inventory").json()["vms"] if item["id"] == "vm-101")
        assert converted["disk_provisioning"] == "thin"
        assert converted["host_id"] == "host-esx03"
        assert converted["storage_used_bytes"] < converted["storage_provisioned_bytes"]


def test_migrate_rejects_over_fifty():
    with TestClient(app) as client:
        ids = [f"vm-{i}" for i in range(51)]
        response = client.post(
            "/api/vms/migrate",
            json={"vm_ids": ids, "host_id": "host-esx03", "confirm": True},
        )
        assert response.status_code == 400
        assert "50" in response.json()["detail"]


def test_catalog_includes_networks():
    with TestClient(app) as client:
        catalog = None
        for _ in range(40):
            catalog = client.get("/api/catalog").json()
            if catalog.get("networks"):
                break
            time.sleep(0.05)
        assert catalog is not None
        assert catalog["networks"]
        assert any(ds.get("host_ids") for ds in catalog["datastores"])


def test_migration_access_grant_in_demo():
    with TestClient(app) as client:
        denied = client.post("/api/migration-access", json={"scope": "cluster", "confirm": False})
        assert denied.status_code == 400
        status = client.get("/api/migration-access").json()
        assert status["privileges"]
        assert status["can_modify_permissions"] is True
        granted = client.post(
            "/api/migration-access",
            json={"scope": "cluster", "cluster_id": "cluster-lab", "confirm": True},
        )
        assert granted.status_code == 200
        assert "vFleet-Migrate" in granted.json()["message"]
