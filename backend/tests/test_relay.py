import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.errors import TransientError, is_permanent, is_transient
from app.main import app
from app.store import LocalStore, backoff_seconds


def test_transient_and_permanent_classification():
    assert is_transient(TimeoutError("timed out"))
    assert is_transient(TransientError("vpn dropped"))
    assert is_permanent(ValueError("file already exists"))
    assert not is_transient(ValueError("duplicate name"))


def test_store_job_retry_and_resume(tmp_path: Path):
    store = LocalStore(tmp_path / "vfleet.db")
    job = store.enqueue("clone", "Clone lab", {"name": "x"}, idempotency_key="clone:x")
    again = store.enqueue("clone", "Clone lab", {"name": "x"}, idempotency_key="clone:x")
    assert again.id == job.id
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == job.id
    store.save_progress(job.id, {"task_id": "task-1", "bytes_sent": 12})
    store.retry(job.id, "connection reset", 1, {"task_id": "task-1", "bytes_sent": 12})
    loaded = store.get(job.id)
    assert loaded is not None
    assert loaded.status == "retrying"
    assert loaded.progress["task_id"] == "task-1"
    assert backoff_seconds(1) == 2
    store.requeue_orphans()
    store.complete(job.id, {"ok": True})
    assert store.get(job.id).status == "succeeded"


def test_catalog_and_jobs_in_demo():
    with TestClient(app) as client:
        catalog = client.get("/api/catalog").json()
        assert len(catalog["datastores"]) >= 2
        assert any(item["name"] == "win11-gold" for item in catalog["templates"])
        listing = client.get(f"/api/datastores/{catalog['datastores'][0]['id']}/files").json()
        assert "files" in listing
        name = f"pytest-win11-from-app-{int(time.time() * 1000) % 100000}"
        queued = client.post(
            "/api/vms",
            json={
                "template_id": catalog["templates"][0]["id"],
                "name": name,
                "datastore_id": catalog["datastores"][1]["id"],
                "cpu_count": 2,
                "memory_mib": 4096,
                "power_on": False,
            },
        )
        assert queued.status_code == 200
        job_id = queued.json()["id"]
        job = None
        for _ in range(80):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)
        assert job is not None
        assert job["status"] == "succeeded", job
        inventory = client.get("/api/inventory").json()
        assert any(vm["name"] == name for vm in inventory["vms"])


def test_actions_queue_locally():
    with TestClient(app) as client:
        response = client.post(
            "/api/actions",
            json={"vm_ids": ["vm-103"], "action": "start", "confirm": True},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["queued"] is True
        assert payload["job_id"]


def test_staging_and_upload_job():
    with TestClient(app) as client:
        catalog = client.get("/api/catalog").json()
        ds = catalog["datastores"][0]["id"]
        created = client.post("/api/staging", json={"filename": "tools.iso", "size": 8})
        assert created.status_code == 200
        staging_id = created.json()["id"]
        put = client.put(
            f"/api/staging/{staging_id}",
            content=b"testdata",
            headers={"Content-Range": "bytes 0-7/8"},
        )
        assert put.json()["complete"] is True
        queued = client.post(
            "/api/uploads",
            json={"staging_id": staging_id, "datastore_id": ds, "remote_path": "isos/tools.iso", "use_library": False},
        )
        assert queued.status_code == 200
        job_id = queued.json()["id"]
        job = None
        for _ in range(80):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)
        assert job is not None
        assert job["status"] == "succeeded", job
        listing = client.get(f"/api/datastores/{ds}/files", params={"path": "isos"}).json()
        assert any(item["name"] == "tools.iso" for item in listing["files"])
