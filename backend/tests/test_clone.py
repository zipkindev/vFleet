from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_clone_rejects_drs_override_without_host():
    with TestClient(app) as client:
        catalog = client.get("/api/catalog").json()
        template = catalog["templates"][0]
        datastore = catalog["datastores"][0]
        response = client.post(
            "/api/vms",
            json={
                "template_id": template["id"],
                "name": "clone-drs-no-host",
                "datastore_id": datastore["id"],
                "disable_drs": True,
            },
        )
        assert response.status_code == 400
        assert "host" in response.json()["detail"].lower()


def test_deploy_accepts_pinned_host_with_drs_override():
    with TestClient(app) as client:
        catalog = client.get("/api/catalog").json()
        inventory = client.get("/api/inventory").json()
        template = catalog["templates"][0]
        datastore = catalog["datastores"][0]
        host = inventory["hosts"][0]
        response = client.post(
            "/api/vms/deploy",
            json={
                "template_id": template["id"],
                "name": "deploy-pinned-drs",
                "datastore_id": datastore["id"],
                "cluster_id": host["cluster_id"],
                "host_id": host["id"],
                "disable_drs": True,
            },
        )
        assert response.status_code == 200
        job = response.json()
        assert job["kind"] == "deploy"
        assert job["payload"]["disable_drs"] is True


def test_clone_accepts_pinned_host_without_drs_override():
    with TestClient(app) as client:
        catalog = client.get("/api/catalog").json()
        inventory = client.get("/api/inventory").json()
        template = catalog["templates"][0]
        datastore = catalog["datastores"][0]
        host = inventory["hosts"][0]
        response = client.post(
            "/api/vms",
            json={
                "template_id": template["id"],
                "name": "clone-pinned-no-drs",
                "datastore_id": datastore["id"],
                "cluster_id": host["cluster_id"],
                "host_id": host["id"],
                "disable_drs": False,
            },
        )
        assert response.status_code == 200
        job = response.json()
        assert job["kind"] == "clone"
        assert job["payload"]["host_id"] == host["id"]
        assert job["payload"]["disable_drs"] is False
