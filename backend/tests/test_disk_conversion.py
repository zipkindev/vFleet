from types import SimpleNamespace

from pyVmomi import vim

from app.adapters.vcenter import VCenterAdapter
from app.config import Settings
from app.models import DiskConversionPlanRequest
from app.store import LocalStore


def _disk_vm() -> SimpleNamespace:
    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
    backing.fileName = "[datastore1] TestVM/TestVM.vmdk"
    backing.thinProvisioned = False
    backing.eagerlyScrub = False
    backing.diskMode = "persistent"
    backing.sharing = "sharingNone"
    disk = vim.vm.device.VirtualDisk()
    disk.key = 2000
    disk.capacityInBytes = 10 * 1024**3
    disk.backing = backing
    disk.deviceInfo = vim.Description(label="Hard disk 1", summary="10 GiB")
    return SimpleNamespace(
        name="TestVM",
        runtime=SimpleNamespace(powerState="poweredOff"),
        config=SimpleNamespace(
            hardware=SimpleNamespace(device=[disk]),
            changeVersion="7",
        ),
        snapshot=None,
    )


def _adapter(*, direct_esxi: bool, ssh_enabled: bool) -> tuple[VCenterAdapter, SimpleNamespace]:
    adapter = VCenterAdapter(Settings(_env_file=None, esxi_ssh_enabled=ssh_enabled))
    vm = _disk_vm()
    adapter._is_esxi = lambda: direct_esxi
    adapter._session = lambda: SimpleNamespace(content=SimpleNamespace())
    adapter._find_vm = lambda _content, _vm_id: vm
    adapter.endpoint_fingerprint = lambda: "endpoint"
    return adapter, vm


def test_auto_disk_conversion_uses_ssh_for_direct_esxi():
    adapter, _vm = _adapter(direct_esxi=True, ssh_enabled=True)
    plan = adapter.disk_conversion_plan(DiskConversionPlanRequest(vm_id="vm-1", target="thin", method="auto"))

    assert plan.method == "ssh"
    assert plan.can_execute is True
    assert any("temporarily starts the ESXi SSH service" in warning for warning in plan.warnings)


def test_auto_disk_conversion_blocks_direct_esxi_until_ssh_is_configured():
    adapter, _vm = _adapter(direct_esxi=True, ssh_enabled=False)
    plan = adapter.disk_conversion_plan(DiskConversionPlanRequest(vm_id="vm-1", target="thin", method="auto"))

    assert plan.method == "ssh"
    assert plan.can_execute is False
    assert any("not configured" in blocker for blocker in plan.blockers)


def test_auto_disk_conversion_keeps_vcenter_on_soap():
    adapter, _vm = _adapter(direct_esxi=False, ssh_enabled=False)
    plan = adapter.disk_conversion_plan(DiskConversionPlanRequest(vm_id="vm-1", target="thin", method="auto"))

    assert plan.method == "soap"
    assert plan.can_execute is True


def test_ssh_conversion_temporarily_starts_and_restores_host_service(monkeypatch):
    adapter, vm = _adapter(direct_esxi=True, ssh_enabled=True)
    events: list[str] = []
    service = SimpleNamespace(key="TSM-SSH", running=False)
    service_system = SimpleNamespace(
        serviceInfo=SimpleNamespace(service=[service]),
        StartService=lambda id: events.append(f"start:{id}"),
        StopService=lambda id: events.append(f"stop:{id}"),
    )
    adapter._host_obj = lambda: SimpleNamespace(
        configManager=SimpleNamespace(serviceSystem=service_system)
    )
    vm.ReconfigVM_Task = lambda spec: SimpleNamespace(_GetMoId=lambda: "task-1")
    adapter.wait_task = lambda _task_id: None
    monkeypatch.setattr("app.esxi_ssh.EsxiSshExecutor.clone_disk", lambda *_args, **_kwargs: None)

    plan = adapter.disk_conversion_plan(DiskConversionPlanRequest(vm_id="vm-1", target="thin", method="auto"))
    result = adapter.execute_ssh_disk_conversion(plan)

    assert events == ["start:TSM-SSH", "stop:TSM-SSH"]
    assert result["ssh_service_temporarily_started"] is True
    assert result["ssh_service_restored"] is True
    assert result["source_disks_preserved"] is True


def test_requeue_disk_conversion_discards_failed_task_attachment(tmp_path):
    store = LocalStore(tmp_path / "jobs.db")
    try:
        job = store.enqueue("disk_convert", "convert", {"plan": {}})
        store.save_progress(job.id, {"task_id": "failed-task", "phase": "relocate"})
        store.fail(job.id, "not supported")

        requeued = store.requeue(job.id)

        assert requeued is not None
        assert requeued.progress.get("task_id") == ""
        assert requeued.progress.get("phase") == ""
    finally:
        store.close()
