import time

from fastapi.testclient import TestClient

from app.main import app


def test_health():
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True


def test_inventory_groups_and_reclaim():
    with TestClient(app) as client:
        response = client.get("/api/inventory")
        assert response.status_code == 200
        payload = response.json()
        owners = {row["owner_key"] for row in payload["owners"]}
        assert "atlasdemo" in owners
        assert "novademo" in owners
        assert any(vm["idle_score"] >= 40 and vm["power_state"] == "POWERED_ON" for vm in payload["vms"])
        atlasdemo = next(row for row in payload["owners"] if row["owner_key"] == "atlasdemo")
        assert atlasdemo["vm_count"] >= 3
        assert atlasdemo["powered_on"] + atlasdemo["powered_off"] + atlasdemo["suspended"] == atlasdemo["vm_count"]
        sample = next(vm for vm in payload["vms"] if vm["id"] == "vm-104")
        assert sample["storage_provisioned_bytes"] > 0
        assert sample["disk_provisioning"] in {"thin", "thick", "mixed"}
        assert {vm["disk_provisioning"] for vm in payload["vms"]} >= {"thin", "thick"}


def test_inventory_owner_filter_matches_deployed_by():
    with TestClient(app) as client:
        response = client.get("/api/inventory", params={"owner": "emberdemo"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["vms"]
        assert all(
            vm["deployed_by"] == "emberdemo" or vm["owner_key"] == "emberdemo" for vm in payload["vms"]
        )
        assert any(vm["owner_key"] == "windows" for vm in payload["vms"])


def test_actions_require_confirm():
    with TestClient(app) as client:
        response = client.post("/api/actions", json={"vm_ids": ["vm-104"], "action": "shutdown", "confirm": False})
        assert response.status_code == 400


def test_metrics_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/metrics?hours=24")
        assert response.status_code == 200
        payload = response.json()
        assert "series" in payload
        assert "owners" in payload
        assert payload["hours"] == 24
        assert isinstance(payload["owners"], list)
        wide = client.get("/api/metrics?hours=336")
        assert wide.status_code == 200
        assert wide.json()["hours"] == 336


def test_destroy_from_disk_in_demo():
    with TestClient(app) as client:
        before = client.get("/api/inventory").json()
        target = next(vm for vm in before["vms"] if vm["id"] == "vm-104")
        queued = client.post(
            "/api/actions",
            json={"vm_ids": [target["id"]], "action": "destroy", "confirm": True},
        )
        assert queued.status_code == 200
        job_id = queued.json()["job_id"]
        job = None
        for _ in range(80):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)
        assert job is not None
        assert job["status"] == "succeeded", job
        after = client.get("/api/inventory").json()
        assert all(vm["id"] != target["id"] for vm in after["vms"])
