from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.errors import humanize_vcenter_error
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


def test_list_vm_folders():
    with TestClient(app) as client:
        response = client.get("/api/folders")
        assert response.status_code == 200
        folders = response.json()
        assert folders
        assert any(item["name"] == "SHARED-Playground" for item in folders)
        assert any(item["name"] == "VM Templates" for item in folders)
        paths = [item["path"] for item in folders]
        assert paths == sorted(paths, key=str.lower)


def test_deploy_into_destination_folder():
    with TestClient(app) as client:
        catalog = client.get("/api/catalog").json()
        folders = client.get("/api/folders").json()
        template = catalog["templates"][0]
        datastore = catalog["datastores"][0]
        playground = next(item for item in folders if item["name"] == "SHARED-Playground")

        queued = client.post(
            "/api/vms/deploy",
            json={
                "template_id": template["id"],
                "name": "folder-deploy-playground",
                "datastore_id": datastore["id"],
                "folder_id": playground["id"],
            },
        )
        assert queued.status_code == 200
        job = _wait_job(client, queued.json()["id"])
        assert job["status"] == "succeeded", job

        inventory = client.get("/api/inventory").json()
        vm = next(item for item in inventory["vms"] if item["name"] == "folder-deploy-playground")
        assert vm["folder_id"] == playground["id"]
        assert "SHARED-Playground" in vm["folder_path"]


def test_deploy_default_keeps_template_folder():
    with TestClient(app) as client:
        catalog = client.get("/api/catalog").json()
        folders = client.get("/api/folders").json()
        template = catalog["templates"][0]
        datastore = catalog["datastores"][0]
        templates_folder = next(item for item in folders if item["name"] == "VM Templates")

        queued = client.post(
            "/api/vms/deploy",
            json={
                "template_id": template["id"],
                "name": "folder-deploy-default",
                "datastore_id": datastore["id"],
            },
        )
        assert queued.status_code == 200
        job = _wait_job(client, queued.json()["id"])
        assert job["status"] == "succeeded", job

        inventory = client.get("/api/inventory").json()
        vm = next(item for item in inventory["vms"] if item["name"] == "folder-deploy-default")
        assert vm["folder_id"] == templates_folder["id"]
        assert "VM Templates" in vm["folder_path"]


def test_clone_migrate_into_destination_folder():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        folders = client.get("/api/folders").json()
        source = next(item for item in inventory["vms"] if item["id"] == "vm-101")
        dest_host = next(
            item
            for item in inventory["hosts"]
            if item["cluster_id"] == source["cluster_id"] and item["id"] != source["host_id"]
        )
        training = next(item for item in folders if item["name"] == "Training-Team")

        queued = client.post(
            "/api/vms/clone-migrate",
            json={
                "vm_id": source["id"],
                "host_id": dest_host["id"],
                "name": "folder-clone-training",
                "folder_id": training["id"],
                "confirm": True,
            },
        )
        assert queued.status_code == 200
        job = _wait_job(client, queued.json()["id"])
        assert job["status"] == "succeeded", job

        after = client.get("/api/inventory").json()
        clone = next(item for item in after["vms"] if item["name"] == "folder-clone-training")
        assert clone["folder_id"] == training["id"]
        assert clone["host_id"] == dest_host["id"]


def test_migrate_folder_only():
    with TestClient(app) as client:
        before = client.get("/api/inventory").json()
        folders = client.get("/api/folders").json()
        vm = next(item for item in before["vms"] if item["id"] == "vm-102")
        support = next(item for item in folders if item["name"] == "Support-Team")
        assert vm["folder_id"] != support["id"]

        queued = client.post(
            "/api/vms/migrate",
            json={"vm_ids": [vm["id"]], "folder_id": support["id"], "confirm": True},
        )
        assert queued.status_code == 200
        job = _wait_job(client, queued.json()["id"])
        assert job["status"] == "succeeded", job

        after = client.get("/api/inventory").json()
        moved = next(item for item in after["vms"] if item["id"] == "vm-102")
        assert moved["folder_id"] == support["id"]
        assert moved["host_id"] == vm["host_id"]


def test_invalid_folder_fails_permanently():
    with TestClient(app) as client:
        catalog = client.get("/api/catalog").json()
        template = catalog["templates"][0]
        datastore = catalog["datastores"][0]
        queued = client.post(
            "/api/vms/deploy",
            json={
                "template_id": template["id"],
                "name": "folder-deploy-bad",
                "datastore_id": datastore["id"],
                "folder_id": "folder-does-not-exist",
            },
        )
        assert queued.status_code == 200
        job = _wait_job(client, queued.json()["id"])
        assert job["status"] == "failed"
        assert "folder" in job["error"].lower()


def test_humanize_edit_cluster_privilege():
    raw = (
        "(vim.fault.NoPermission) { msg = 'Permission to perform this operation was denied.', "
        "privilegeId = 'Host.Inventory.EditCluster' }"
    )
    text = humanize_vcenter_error(raw)
    assert "Host.Inventory.EditCluster" in text
    assert "DRS" in text or "cluster" in text.lower()
