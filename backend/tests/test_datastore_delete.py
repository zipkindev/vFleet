from types import SimpleNamespace

import pytest

from app.adapters.vcenter import VCenterAdapter
from app.errors import PermanentError


class Recorder:
    def __init__(self) -> None:
        self.calls = []

    def DeleteVirtualDisk_Task(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(_GetMoId=lambda: "task-disk-delete")

    def DeleteDatastoreFile_Task(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(_GetMoId=lambda: "task-file-delete")


def adapter_with_recorders():
    adapter = object.__new__(VCenterAdapter)
    disk_manager = Recorder()
    file_manager = Recorder()
    content = SimpleNamespace(virtualDiskManager=disk_manager, fileManager=file_manager)
    waited = []
    adapter._obj = lambda _kind, _moid: SimpleNamespace(name="datastore1")
    adapter._find_datacenter = lambda _datastore: None
    adapter._session = lambda: SimpleNamespace(content=content)
    adapter.wait_task = waited.append
    return adapter, disk_manager, file_manager, waited


def test_vmdk_delete_uses_virtual_disk_manager():
    adapter, disk_manager, file_manager, waited = adapter_with_recorders()

    adapter.delete_file("ds-1", "vm/original.vmdk")

    assert disk_manager.calls == [{"name": "[datastore1] vm/original.vmdk", "datacenter": None}]
    assert file_manager.calls == []
    assert waited == ["task-disk-delete"]


def test_non_disk_delete_uses_file_manager():
    adapter, disk_manager, file_manager, waited = adapter_with_recorders()

    adapter.delete_file("ds-1", "iso/installer.iso")

    assert disk_manager.calls == []
    assert file_manager.calls == [{"name": "[datastore1] iso/installer.iso", "datacenter": None}]
    assert waited == ["task-file-delete"]


def test_vmdk_delete_fails_closed_without_virtual_disk_manager():
    adapter, _disk_manager, file_manager, waited = adapter_with_recorders()
    adapter._session = lambda: SimpleNamespace(
        content=SimpleNamespace(virtualDiskManager=None, fileManager=file_manager)
    )

    with pytest.raises(PermanentError, match="safe virtual-disk deletion"):
        adapter.delete_file("ds-1", "vm/original.vmdk")

    assert file_manager.calls == []
    assert waited == []
