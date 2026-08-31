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


def test_rename_queues_and_updates_inventory():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        target = inventory["vms"][0]
        new_name = f"{target['name']}-renamed"
        queued = client.post(
            f"/api/vms/{target['id']}/rename",
            json={"name": new_name, "confirm": True},
        )
        assert queued.status_code == 200
        job = _wait_job(client, queued.json()["id"])
        assert job["status"] == "succeeded", job
        refreshed = client.get("/api/inventory").json()
        updated = next(vm for vm in refreshed["vms"] if vm["id"] == target["id"])
        assert updated["name"] == new_name


def test_rename_requires_confirm():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        vm = inventory["vms"][0]
        response = client.post(
            f"/api/vms/{vm['id']}/rename",
            json={"name": f"{vm['name']}-x", "confirm": False},
        )
        assert response.status_code == 400


def test_rename_rejects_duplicate_and_same_name():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        first, second = inventory["vms"][0], inventory["vms"][1]
        same = client.post(
            f"/api/vms/{first['id']}/rename",
            json={"name": first["name"], "confirm": True},
        )
        assert same.status_code == 400
        dup = client.post(
            f"/api/vms/{first['id']}/rename",
            json={"name": second["name"], "confirm": True},
        )
        assert dup.status_code == 400
        slash = client.post(
            f"/api/vms/{first['id']}/rename",
            json={"name": "bad/name", "confirm": True},
        )
        assert slash.status_code == 400
