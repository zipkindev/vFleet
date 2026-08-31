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


def test_clone_migrate_requires_confirm_and_host():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        vm = inventory["vms"][0]
        host = next(h for h in inventory["hosts"] if h["id"] != vm["host_id"] and h["cluster_id"] == vm["cluster_id"])
        missing_confirm = client.post(
            "/api/vms/clone-migrate",
            json={"vm_id": vm["id"], "host_id": host["id"], "name": f"{vm['name']}-clone-x", "confirm": False},
        )
        assert missing_confirm.status_code == 400
        missing_host = client.post(
            "/api/vms/clone-migrate",
            json={"vm_id": vm["id"], "host_id": "", "name": f"{vm['name']}-clone-y", "confirm": True},
        )
        assert missing_host.status_code == 400


def test_clone_migrate_just_clone_keeps_source():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        vm = next(item for item in inventory["vms"] if item["cluster_id"])
        host = next(h for h in inventory["hosts"] if h["id"] != vm["host_id"] and h["cluster_id"] == vm["cluster_id"])
        clone_name = f"{vm['name']}-just-clone"
        queued = client.post(
            "/api/vms/clone-migrate",
            json={
                "vm_id": vm["id"],
                "host_id": host["id"],
                "name": clone_name,
                "destroy_source": False,
                "disable_drs": True,
                "confirm": True,
            },
        )
        assert queued.status_code == 200, queued.text
        job = _wait_job(client, queued.json()["id"])
        assert job["status"] == "succeeded", job
        refreshed = client.get("/api/inventory").json()
        names = {item["name"]: item for item in refreshed["vms"]}
        assert vm["name"] in names
        assert clone_name in names
        assert names[clone_name]["host_id"] == host["id"]
        assert names[clone_name]["drs_override"] is True


def test_clone_migrate_destroy_replaces_on_host():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        candidates = [item for item in inventory["vms"] if item["name"] == "mzipkin-k8s-cp"]
        vm = candidates[0] if candidates else next(item for item in inventory["vms"] if item["cluster_id"])
        host = next(h for h in inventory["hosts"] if h["id"] != vm["host_id"] and h["cluster_id"] == vm["cluster_id"])
        original = vm["name"]
        source_id = vm["id"]
        queued = client.post(
            "/api/vms/clone-migrate",
            json={
                "vm_id": source_id,
                "host_id": host["id"],
                "destroy_source": True,
                "disable_drs": True,
                "confirm": True,
            },
        )
        assert queued.status_code == 200, queued.text
        job = _wait_job(client, queued.json()["id"])
        assert job["status"] == "succeeded", job
        refreshed = client.get("/api/inventory").json()
        by_id = {item["id"]: item for item in refreshed["vms"]}
        assert source_id not in by_id
        replacement = next(item for item in refreshed["vms"] if item["name"] == original)
        assert replacement["host_id"] == host["id"]
        assert replacement["drs_override"] is True