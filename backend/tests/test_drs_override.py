from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import app


def _wait_job(client: TestClient, job_id: str) -> dict:
    job = None
    for _ in range(80):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert job is not None
    return job


def test_drs_override_queues_and_updates_inventory():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        target = next(vm for vm in inventory["vms"] if vm["cluster_id"] and not vm.get("drs_override"))
        queued = client.post(
            "/api/vms/drs-override",
            json={"vm_ids": [target["id"]], "confirm": True},
        )
        assert queued.status_code == 200
        job = _wait_job(client, queued.json()["id"])
        assert job["status"] == "succeeded", job
        refreshed = client.get("/api/inventory").json()
        updated = next(vm for vm in refreshed["vms"] if vm["id"] == target["id"])
        assert updated["drs_override"] is True


def test_drs_override_requires_confirm():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        vm_id = inventory["vms"][0]["id"]
        response = client.post("/api/vms/drs-override", json={"vm_ids": [vm_id], "confirm": False})
        assert response.status_code == 400
