from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from pyVmomi import vim

from app.adapters.vcenter import VCenterAdapter
from app.config import Settings
from app.errors import PermanentError
from app.main import app
from app.models import VmHardwareRequest
from app.relay import RelayWorker
from app.store import LocalStore

GIB = 1024**3


def _wait_job(client: TestClient, job_id: str) -> dict:
    job = None
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert job is not None
    return job


def _queue_plan(client: TestClient, body: dict) -> tuple[dict, dict]:
    planned = client.post("/api/vms/hardware/plan", json=body)
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    queued = client.post(
        "/api/vms/hardware",
        json={**body, "plan_token": plan["plan_token"], "confirm": True},
    )
    assert queued.status_code == 200, queued.text
    return plan, queued.json()


def test_bulk_cpu_memory_plan_skips_powered_on_without_hot_add_and_updates_eligible_vm():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        powered_on = next(vm for vm in inventory["vms"] if vm["power_state"] == "POWERED_ON")
        powered_off = next(vm for vm in inventory["vms"] if vm["power_state"] == "POWERED_OFF")
        target_cpu = max(powered_on["cpu_count"], powered_off["cpu_count"]) + 2
        target_memory = max(powered_on["memory_mib"], powered_off["memory_mib"]) + 2048
        body = {
            "vm_ids": [powered_on["id"], powered_off["id"]],
            "cpu_count": target_cpu,
            "memory_mib": target_memory,
        }
        plan, queued = _queue_plan(client, body)
        assert plan["can_execute_count"] == 1
        assert plan["blocked_count"] == 1
        blocked = next(row for row in plan["targets"] if row["vm_id"] == powered_on["id"])
        assert any("hot-add" in message for message in blocked["blockers"])

        job = _wait_job(client, queued["id"])
        assert job["status"] == "succeeded", job
        assert job["result"]["requested"] == 1
        refreshed = client.get("/api/inventory").json()
        by_id = {vm["id"]: vm for vm in refreshed["vms"]}
        assert by_id[powered_off["id"]]["cpu_count"] == target_cpu
        assert by_id[powered_off["id"]]["memory_mib"] == target_memory
        assert by_id[powered_on["id"]]["cpu_count"] == powered_on["cpu_count"]


def test_power_workflow_shuts_down_reconfigures_and_restores_originally_running_vm():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        target = next(
            vm
            for vm in inventory["vms"]
            if vm["power_state"] == "POWERED_ON"
            and vm["tools_status"] in {"toolsOk", "guestToolsRunning"}
            and vm["cpu_count"] > 1
        )
        body = {
            "vm_ids": [target["id"]],
            "cpu_count": target["cpu_count"] - 1,
            "shutdown_before": True,
            "power_on_after": True,
        }
        plan, queued = _queue_plan(client, body)
        assert plan["can_execute_count"] == 1
        assert plan["blocked_count"] == 0
        assert plan["targets"][0]["will_shutdown_before"] is True
        assert plan["targets"][0]["will_power_on_after"] is True

        job = _wait_job(client, queued["id"])
        assert job["status"] == "succeeded", job
        result = job["result"]["results"][0]
        assert result["shutdown_requested"] is True
        assert result["forced_power_off"] is False
        assert result["powered_on_after"] is True

        refreshed = client.get("/api/inventory").json()
        updated = next(vm for vm in refreshed["vms"] if vm["id"] == target["id"])
        assert updated["cpu_count"] == target["cpu_count"] - 1
        assert updated["power_state"] == "POWERED_ON"


def test_force_power_off_fallback_is_explicit_and_restores_vm_without_tools():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        target = next(
            vm
            for vm in inventory["vms"]
            if vm["power_state"] == "POWERED_ON"
            and vm["tools_status"] not in {"toolsOk", "guestToolsRunning"}
            and vm["cpu_count"] > 1
        )
        base = {
            "vm_ids": [target["id"]],
            "cpu_count": target["cpu_count"] - 1,
            "shutdown_before": True,
            "power_on_after": True,
        }
        blocked = client.post("/api/vms/hardware/plan", json=base)
        assert blocked.status_code == 200
        assert blocked.json()["blocked_count"] == 1
        assert any("VMware Tools" in item for item in blocked.json()["targets"][0]["blockers"])

        plan, queued = _queue_plan(client, {**base, "force_power_off_on_timeout": True})
        assert plan["can_execute_count"] == 1
        assert plan["targets"][0]["may_force_power_off"] is True
        job = _wait_job(client, queued["id"])
        assert job["status"] == "succeeded", job
        result = job["result"]["results"][0]
        assert result["shutdown_requested"] is False
        assert result["forced_power_off"] is True
        assert result["powered_on_after"] is True

        refreshed = client.get("/api/inventory").json()
        updated = next(vm for vm in refreshed["vms"] if vm["id"] == target["id"])
        assert updated["cpu_count"] == target["cpu_count"] - 1
        assert updated["power_state"] == "POWERED_ON"


def test_disk_growth_and_iso_mount_are_applied_in_one_reconfiguration():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        target = next(vm for vm in inventory["vms"] if vm["power_state"] == "POWERED_OFF")
        disk = target["disks"][0]
        body = {
            "vm_ids": [target["id"]],
            "disk_index": 0,
            "disk_capacity_bytes": disk["capacity_bytes"] + 10 * GIB,
            "iso_action": "mount",
            "iso_datastore_id": "ds-iso",
            "iso_path": "isos/rhel9.iso",
            "iso_connect_at_power_on": True,
        }
        plan, queued = _queue_plan(client, body)
        assert plan["can_execute_count"] == 1
        assert len(plan["targets"][0]["changes"]) == 2
        job = _wait_job(client, queued["id"])
        assert job["status"] == "succeeded", job

        refreshed = client.get("/api/inventory").json()
        updated = next(vm for vm in refreshed["vms"] if vm["id"] == target["id"])
        assert updated["disks"][0]["capacity_bytes"] == disk["capacity_bytes"] + 10 * GIB
        assert updated["mounted_iso_path"] == "[ISO] isos/rhel9.iso"
        assert updated["iso_start_connected"] is True


def test_hardware_requires_confirm_and_rejects_stale_plan_token():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        target = next(vm for vm in inventory["vms"] if vm["power_state"] == "POWERED_OFF")
        body = {"vm_ids": [target["id"]], "cpu_count": target["cpu_count"] + 1}
        planned = client.post("/api/vms/hardware/plan", json=body)
        assert planned.status_code == 200
        assert client.post(
            "/api/vms/hardware",
            json={**body, "plan_token": planned.json()["plan_token"], "confirm": False},
        ).status_code == 400
        stale = client.post(
            "/api/vms/hardware",
            json={**body, "plan_token": "stale", "confirm": True},
        )
        assert stale.status_code == 409


def test_power_workflow_flags_require_shutdown_before():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        target = inventory["vms"][0]
        response = client.post(
            "/api/vms/hardware/plan",
            json={
                "vm_ids": [target["id"]],
                "cpu_count": target["cpu_count"] + 1,
                "force_power_off_on_timeout": True,
                "power_on_after": True,
            },
        )
        assert response.status_code == 400
        assert "requires guest shutdown" in response.text


def test_hardware_rejects_disk_shrink_and_invalid_iso_path():
    with TestClient(app) as client:
        inventory = client.get("/api/inventory").json()
        target = inventory["vms"][0]
        disk = target["disks"][0]
        shrink = client.post(
            "/api/vms/hardware/plan",
            json={
                "vm_ids": [target["id"]],
                "disk_index": 0,
                "disk_capacity_bytes": disk["capacity_bytes"] - GIB,
            },
        )
        assert shrink.status_code == 200
        assert any("cannot be shrunk" in item for item in shrink.json()["targets"][0]["blockers"])
        invalid_iso = client.post(
            "/api/vms/hardware/plan",
            json={
                "vm_ids": [target["id"]],
                "iso_action": "mount",
                "iso_datastore_id": "ds-iso",
                "iso_path": "../secret.iso",
            },
        )
        assert invalid_iso.status_code == 400
        missing_iso = client.post(
            "/api/vms/hardware/plan",
            json={
                "vm_ids": [target["id"]],
                "iso_action": "mount",
                "iso_datastore_id": "ds-iso",
                "iso_path": "isos/missing.iso",
            },
        )
        assert missing_iso.status_code == 200
        assert any("not found" in item for item in missing_iso.json()["targets"][0]["blockers"])


def _vcenter_hardware_adapter(power_state: str = "poweredOff") -> tuple[VCenterAdapter, SimpleNamespace, list]:
    disk_backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
    disk_backing.fileName = "[data] test/test.vmdk"
    disk_backing.diskMode = "persistent"
    disk_backing.sharing = "sharingNone"
    disk = vim.vm.device.VirtualDisk()
    disk.key = 2000
    disk.capacityInBytes = 10 * GIB
    disk.backing = disk_backing
    disk.deviceInfo = vim.Description(label="Hard disk 1", summary="10 GiB")
    cdrom = vim.vm.device.VirtualCdrom()
    cdrom.key = 3000
    cdrom.backing = vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo()
    cdrom.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
        connected=False,
        startConnected=False,
        allowGuestControl=True,
    )
    captured: list = []
    vm = SimpleNamespace(
        name="test",
        runtime=SimpleNamespace(
            powerState=power_state,
            toolsInstallerMounted=False,
            host=SimpleNamespace(_GetMoId=lambda: "host-1", name="esx01"),
        ),
        config=SimpleNamespace(
            template=False,
            cpuHotAddEnabled=False,
            memoryHotAddEnabled=False,
            hardware=SimpleNamespace(
                numCPU=2,
                numCoresPerSocket=2,
                memoryMB=2048,
                device=[disk, cdrom],
            ),
        ),
        ReconfigVM_Task=lambda spec: captured.append(spec) or SimpleNamespace(_GetMoId=lambda: "task-1"),
    )
    adapter = VCenterAdapter(Settings(_env_file=None))
    adapter._session = lambda: SimpleNamespace(content=SimpleNamespace())
    adapter._find_vm = lambda _content, _vm_id: vm
    class DatastoreStub:
        def InvokeAccessor(self, _managed_object, info):
            return [] if info.name == "host" else "ISO"

    datastore = vim.Datastore("ds-iso", DatastoreStub())
    adapter._obj = lambda _kind, _moid: datastore
    return adapter, vm, captured


def test_vcenter_reconfigure_builds_one_atomic_config_spec():
    adapter, vm, captured = _vcenter_hardware_adapter()
    request = VmHardwareRequest(
        vm_ids=["vm-1"],
        cpu_count=3,
        memory_mib=4096,
        disk_index=0,
        disk_capacity_bytes=20 * GIB,
        iso_action="mount",
        iso_datastore_id="ds-iso",
        iso_path="isos/rhel9.iso",
        plan_token="reviewed",
        confirm=True,
    )
    task_id = adapter.start_vm_reconfigure("vm-1", request)

    assert task_id == "task-1"
    assert len(captured) == 1
    config = captured[0]
    assert config.numCPUs == 3
    assert config.numCoresPerSocket == 1
    assert config.memoryMB == 4096
    assert len(config.deviceChange) == 2
    assert vm.config.hardware.device[0].capacityInKB == 20 * GIB // 1024
    assert vm.config.hardware.device[1].backing.fileName == "[ISO] isos/rhel9.iso"
    assert vm.config.hardware.device[1].connectable.startConnected is True


def test_vcenter_reconfigure_rechecks_hot_add_at_execution_time():
    adapter, _vm, _captured = _vcenter_hardware_adapter("poweredOn")
    request = VmHardwareRequest(
        vm_ids=["vm-1"],
        cpu_count=4,
        iso_action="keep",
        plan_token="reviewed",
        confirm=True,
    )
    with pytest.raises(PermanentError, match="CPU hot-add"):
        adapter.start_vm_reconfigure("vm-1", request)


def test_vcenter_power_workflow_exposes_guest_shutdown_and_reattachable_power_tasks():
    adapter, vm, _captured = _vcenter_hardware_adapter("poweredOn")
    calls = []
    vm.ShutdownGuest = lambda: calls.append("shutdown")
    vm.PowerOff = lambda: SimpleNamespace(_GetMoId=lambda: "task-off")
    vm.PowerOn = lambda: SimpleNamespace(_GetMoId=lambda: "task-on")

    assert adapter.vm_power_state("vm-1") == "POWERED_ON"
    adapter.request_guest_shutdown("vm-1")
    assert calls == ["shutdown"]
    assert adapter.start_vm_power("vm-1", power_on=False) == "task-off"
    vm.runtime.powerState = "poweredOff"
    assert adapter.start_vm_power("vm-1", power_on=False) == ""
    assert adapter.start_vm_power("vm-1", power_on=True) == "task-on"


def test_manual_retry_restarts_failed_bulk_hardware_progress(tmp_path):
    store = LocalStore(tmp_path / "jobs.db")
    try:
        job = store.enqueue("vm_hardware", "configure", {"vm_ids": ["vm-1", "vm-2"]})
        store.save_progress(job.id, {"index": 2, "phase": "start", "results": [{"vm_id": "vm-1", "ok": False}]})
        store.fail(job.id, "all failed")
        retried = store.requeue(job.id)
        assert retried is not None
        assert retried.progress == {}
    finally:
        store.close()


def test_relay_keeps_bulk_per_vm_failure_and_continues(tmp_path):
    store = LocalStore(tmp_path / "jobs.db")

    class Adapter:
        def endpoint_fingerprint(self):
            return ""

        def start_vm_reconfigure(self, vm_id, _spec):
            if vm_id == "vm-1":
                raise PermanentError("unsupported hardware")
            return ""

        def wait_task(self, _task_id):
            return None

    try:
        worker = RelayWorker(Settings(_env_file=None, data_dir=tmp_path), store, Adapter)
        job = store.enqueue(
            "vm_hardware",
            "configure",
            {"vm_ids": ["vm-1", "vm-2"], "cpu_count": 2, "iso_action": "keep", "plan_token": "reviewed", "confirm": True},
        )
        result = worker._execute(job)
        assert result["succeeded"] == 1
        assert result["results"] == [
            {"vm_id": "vm-1", "ok": False, "error": "unsupported hardware"},
            {"vm_id": "vm-2", "ok": True, "task_id": ""},
        ]
    finally:
        store.close()


def test_relay_restores_power_after_a_failed_hardware_change(tmp_path):
    store = LocalStore(tmp_path / "jobs.db")

    class Adapter:
        def __init__(self):
            self.state = "POWERED_ON"

        def endpoint_fingerprint(self):
            return ""

        def vm_power_state(self, _vm_id):
            return self.state

        def request_guest_shutdown(self, _vm_id):
            self.state = "POWERED_OFF"

        def wait_vm_power_state(self, _vm_id, target, _timeout):
            return self.state == target

        def start_vm_reconfigure(self, _vm_id, _spec):
            raise PermanentError("unsupported hardware")

        def start_vm_power(self, _vm_id, *, power_on):
            self.state = "POWERED_ON" if power_on else "POWERED_OFF"
            return ""

        def wait_task(self, _task_id):
            return None

    adapter = Adapter()
    try:
        worker = RelayWorker(Settings(_env_file=None, data_dir=tmp_path), store, lambda: adapter)
        job = store.enqueue(
            "vm_hardware",
            "configure",
            {
                "vm_ids": ["vm-1"],
                "cpu_count": 2,
                "iso_action": "keep",
                "shutdown_before": True,
                "power_on_after": True,
                "plan_token": "reviewed",
                "confirm": True,
            },
        )
        with pytest.raises(PermanentError, match="unsupported hardware"):
            worker._execute(job)
        saved = store.get(job.id)
        assert saved is not None
        assert saved.progress["results"][0]["powered_on_after"] is True
        assert saved.progress["results"][0]["ok"] is False
        assert adapter.state == "POWERED_ON"
    finally:
        store.close()
