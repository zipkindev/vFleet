from __future__ import annotations

from fastapi.testclient import TestClient

from app.adapters.vcenter import VCenterAdapter
from app.main import app


def test_console_requires_live_vcenter_in_demo():
    with TestClient(app) as client:
        response = client.post("/api/vms/vm-104/console", json={"type": "vmrc"})
        assert response.status_code == 400
        assert "live vCenter" in response.json()["detail"]


def test_console_rejects_unknown_type():
    with TestClient(app) as client:
        response = client.post("/api/vms/vm-104/console", json={"type": "vnc"})
        assert response.status_code == 400
        assert "vmrc or webmks" in response.json()["detail"]


def test_console_unknown_vm_in_demo():
    with TestClient(app) as client:
        response = client.post("/api/vms/vm-missing/console", json={"type": "vmrc"})
        assert response.status_code == 400
        assert "not found" in response.json()["detail"].lower()


def test_vcenter_console_url_helper():
    adapter = VCenterAdapter.__new__(VCenterAdapter)
    adapter.settings = type("Settings", (), {"vcenter_host": "vc.lab.local", "vcenter_port": 443})()
    url = adapter._vcenter_console_url("vm-104")
    assert url == "https://vc.lab.local/ui/app/vm;nav=s/urn:vmomi:VirtualMachine:vm-104/console"


def test_normalize_console_ticket_vmrc_uri():
    adapter = VCenterAdapter.__new__(VCenterAdapter)
    payload = {"ticket": "vmrc://clone:vm-104@vc.lab.local/?moid=vm-104"}
    normalized = adapter._normalize_console_ticket(payload, "vmrc")
    assert normalized["uri"].startswith("vmrc://")


def test_normalize_console_ticket_webmks_wss():
    adapter = VCenterAdapter.__new__(VCenterAdapter)
    payload = {"ticket": "abc123", "host": "esxi1.lab.local", "port": 902}
    normalized = adapter._normalize_console_ticket(payload, "webmks")
    assert normalized["uri"] == "wss://esxi1.lab.local:902/ticket/abc123"
