from datetime import timedelta

from fastapi.testclient import TestClient

from app.adapters.demo import DemoAdapter
from app.config import Settings
from app.main import app
from app.models import DatastoreFile, VirtualDiskSummary
from app.storage_reconciliation import build_storage_reconciliation
from app.store import LocalStore


def _conversion_fixture(tmp_path):
    adapter = DemoAdapter(Settings(_env_file=None, data_dir=tmp_path))
    store = LocalStore(tmp_path / "reconcile.db")
    vm = next(item for item in adapter._vms.values() if item.power_state == "POWERED_ON")
    destination = f"[{adapter._datastores['ds-lab'].name}] {vm.name}/{vm.name}.vfleet-test.vmdk"
    source = f"[{adapter._datastores['ds-lab'].name}] {vm.name}/{vm.name}.vmdk"
    vm.disks = [
        VirtualDiskSummary(
            key=2000,
            label="Hard disk 1",
            capacity_bytes=40 * 1024**3,
            file_name=destination,
            datastore_id="ds-lab",
            datastore_name=adapter._datastores["ds-lab"].name,
            provisioning="thin",
        )
    ]
    adapter._files["ds-lab"].extend(
        [
            DatastoreFile(name=vm.name, path=vm.name, is_directory=True, kind="folder"),
            DatastoreFile(name=f"{vm.name}.vfleet-test.vmdk", path=f"{vm.name}/{vm.name}.vfleet-test.vmdk", size=8 * 1024**3, kind="disk"),
            DatastoreFile(name=f"{vm.name}.vmdk", path=f"{vm.name}/{vm.name}.vmdk", size=40 * 1024**3, kind="disk"),
        ]
    )
    job = store.enqueue(
        "disk_convert",
        "convert",
        {"_endpoint_fingerprint": "demo", "plan": {"vm_id": vm.id}},
    )
    store.complete(
        job.id,
        {
            "vm_id": vm.id,
            "converted": [{"source": source, "destination": destination}],
            "source_disks_preserved": True,
        },
    )
    return adapter, store, vm, store.get(job.id)


def test_preserved_conversion_source_requires_manual_validation_without_tools(tmp_path):
    adapter, store, vm, job = _conversion_fixture(tmp_path)
    try:
        vm.tools_status = "guestToolsNotRunning"
        vm.boot_time = job.updated_at + timedelta(seconds=1)

        report = build_storage_reconciliation(adapter, store, vm_ids=[vm.id])

        candidate = next(item for item in report.candidates if item.kind == "preserved_source_disk")
        assert candidate.confidence == "high"
        assert candidate.validation_status == "manual_required"
        assert candidate.can_delete is False
        assert "Deploy Tools" in candidate.warning
        assert report.vm_statuses[0].manual_validation_required is True
    finally:
        store.close()


def test_preserved_conversion_source_is_eligible_after_tools_post_boot_validation(tmp_path):
    adapter, store, vm, job = _conversion_fixture(tmp_path)
    try:
        vm.tools_status = "guestToolsRunning"
        vm.boot_time = job.updated_at + timedelta(seconds=1)

        report = build_storage_reconciliation(adapter, store, vm_ids=[vm.id])

        candidate = next(item for item in report.candidates if item.kind == "preserved_source_disk")
        assert candidate.validation_status == "automated"
        assert candidate.can_delete is True
        assert report.vm_statuses[0].boot_validated is True
    finally:
        store.close()


def test_full_reconciliation_inventories_unregistered_vm_directory(tmp_path):
    adapter = DemoAdapter(Settings(_env_file=None, data_dir=tmp_path))
    store = LocalStore(tmp_path / "reconcile.db")
    try:
        adapter._files["ds-lab"].extend(
            [
                DatastoreFile(name="AbandonedVM", path="AbandonedVM", is_directory=True, kind="folder"),
                DatastoreFile(name="AbandonedVM.vmx", path="AbandonedVM/AbandonedVM.vmx", size=4096, kind="file"),
                DatastoreFile(name="AbandonedVM.vmdk", path="AbandonedVM/AbandonedVM.vmdk", size=12 * 1024**3, kind="disk"),
            ]
        )

        report = build_storage_reconciliation(adapter, store)

        candidate = next(item for item in report.candidates if item.path == "AbandonedVM")
        assert candidate.kind == "unregistered_vm_directory"
        assert candidate.confidence == "review"
        assert candidate.can_delete is False
        assert "intentionally unregistered" in candidate.warning
    finally:
        store.close()


def test_snapshot_backing_directories_are_excluded_from_unattached_disk_cleanup(tmp_path):
    adapter = DemoAdapter(Settings(_env_file=None, data_dir=tmp_path))
    store = LocalStore(tmp_path / "reconcile.db")
    try:
        vm = next(item for item in adapter._vms.values() if item.power_state == "POWERED_OFF")
        vm.disks = [
            VirtualDiskSummary(
                key=2000,
                label="Hard disk 1",
                capacity_bytes=40 * 1024**3,
                file_name=f"[Lab-SAS] {vm.name}/{vm.name}-000002.vmdk",
                datastore_id="ds-lab",
                datastore_name="Lab-SAS",
                provisioning="thin",
                parent_depth=2,
            )
        ]
        adapter._files["ds-lab"].extend(
            [
                DatastoreFile(name=vm.name, path=vm.name, is_directory=True, kind="folder"),
                DatastoreFile(name=f"{vm.name}.vmdk", path=f"{vm.name}/{vm.name}.vmdk", size=40 * 1024**3, kind="disk"),
                DatastoreFile(name=f"{vm.name}-000001.vmdk", path=f"{vm.name}/{vm.name}-000001.vmdk", size=2 * 1024**3, kind="disk"),
                DatastoreFile(name=f"{vm.name}-000002.vmdk", path=f"{vm.name}/{vm.name}-000002.vmdk", size=2 * 1024**3, kind="disk"),
            ]
        )

        report = build_storage_reconciliation(adapter, store, vm_ids=[vm.id])

        assert report.candidates == []
        assert any("snapshot backing chain" in warning for warning in report.warnings)
    finally:
        store.close()


def test_cleanup_endpoint_requires_explicit_confirmation():
    with TestClient(app) as client:
        response = client.post(
            "/api/storage/reconciliation/cleanup",
            json={
                "candidate_ids": ["candidate"],
                "manual_validated_candidate_ids": [],
                "plan_token": "stale",
                "confirm": False,
            },
        )
        assert response.status_code == 400
        assert "confirm=true" in response.json()["detail"]
