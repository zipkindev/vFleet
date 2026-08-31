import threading
from types import SimpleNamespace

from app.adapters.vcenter import VCenterAdapter


def adapter_for_vm(vm):
    adapter = object.__new__(VCenterAdapter)
    adapter._lock = threading.RLock()
    adapter._cache = object()
    adapter._session = lambda: SimpleNamespace(content=object())
    adapter._find_vm = lambda _content, _vm_id: vm
    return adapter


def test_mount_tools_installer_uses_vsphere_vm_method():
    calls = []
    vm = SimpleNamespace(
        name="Server22Core",
        runtime=SimpleNamespace(powerState="poweredOn", toolsInstallerMounted=False),
        MountToolsInstaller=lambda: calls.append("mounted"),
    )

    result = adapter_for_vm(vm).apply_actions(["16"], "mount_tools")

    assert calls == ["mounted"]
    assert result[0].ok
    assert "first-time installation" in result[0].message


def test_mount_tools_installer_is_idempotent_when_already_mounted():
    calls = []
    vm = SimpleNamespace(
        name="Server22Core",
        runtime=SimpleNamespace(powerState="poweredOn", toolsInstallerMounted=True),
        MountToolsInstaller=lambda: calls.append("mounted"),
    )

    result = adapter_for_vm(vm).apply_actions(["16"], "mount_tools")

    assert calls == []
    assert result[0].ok
    assert result[0].message == "VMware Tools installer is already mounted"
