from app.power import normalize_power_state
from app.reclaim import build_owner_reports
from app.models import VirtualMachine
from app.adapters.demo import DemoAdapter
from app.config import settings


def test_normalize_vsphere_power_states():
    assert normalize_power_state("poweredOn") == "POWERED_ON"
    assert normalize_power_state("poweredOff") == "POWERED_OFF"
    assert normalize_power_state("suspended") == "SUSPENDED"
    assert normalize_power_state("POWERED_ON") == "POWERED_ON"


def test_owner_report_counts_vsphere_states():
    vms = [
        VirtualMachine(
            id="1",
            name="a",
            power_state="poweredOn",
            cpu_count=2,
            memory_mib=4096,
            owner_key="user",
            owner_source="name_prefix",
        ),
        VirtualMachine(
            id="2",
            name="b",
            power_state="poweredOff",
            cpu_count=2,
            memory_mib=4096,
            owner_key="user",
            owner_source="name_prefix",
        ),
        VirtualMachine(
            id="3",
            name="c",
            power_state="suspended",
            cpu_count=2,
            memory_mib=4096,
            owner_key="user",
            owner_source="name_prefix",
        ),
    ]
    report = build_owner_reports(vms)[0]
    assert report.vm_count == 3
    assert report.powered_on == 1
    assert report.powered_off == 1
    assert report.suspended == 1


def test_demo_destroy_removes_vm_and_protects_templates():
    adapter = DemoAdapter(settings)
    gone = adapter.apply_actions(["vm-104"], "destroy")
    assert gone[0].ok
    assert all(vm.id != "vm-104" for vm in adapter.snapshot().vms)
    blocked = adapter.apply_actions(["vm-501"], "destroy")
    assert not blocked[0].ok
    assert "template" in blocked[0].message.lower()
    assert any(item.id == "vm-501" for item in adapter.list_templates())
