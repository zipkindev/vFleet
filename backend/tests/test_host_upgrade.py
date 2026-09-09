from __future__ import annotations

import copy
import hashlib
import struct
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config import Settings
from app.errors import PermanentError, TransientError
from app.host_upgrade import (UpgradePrepareRequest, inspect_plan, load_run, prepare_host,
                              recover_host, save_run, validate_prepare)
from app.store import LocalStore
from app.upgrade_media import inspect_esxi_iso


def iso_bytes(build="24677879"):
    data = bytearray(48 * 2048)
    def record(block, length, name, directory=False):
        raw = bytearray(33 + len(name) + (0 if len(name) % 2 else 1))
        raw[0] = len(raw)
        struct.pack_into("<I", raw, 2, block)
        struct.pack_into(">I", raw, 6, block)
        struct.pack_into("<I", raw, 10, length)
        struct.pack_into(">I", raw, 14, length)
        raw[25] = 2 if directory else 0
        raw[32] = len(name)
        raw[33:33 + len(name)] = name
        return raw
    pvd = bytearray(2048)
    pvd[:7] = b"\x01CD001\x01"
    struct.pack_into("<H", pvd, 128, 2048)
    pvd[156:190] = record(20, 2048, b"\x00", True)
    data[16 * 2048:17 * 2048] = pvd
    cfg = (f"kernel=b.b00\nkernelopt=runweasel\nmodules=/weaselin.v00 --- /esxupdt.v00 --- "
           f"/imgdb.tgz --- /imgpayld.tgz\nbuild=8.0.3-0.70.{build}\n").encode()
    root_files = [(b"BOOT.CFG;1", 21, cfg), (b"B.B00;1", 22, b"kernel00"),
                  (b"WEASELIN.V00;1", 23, b"installer"), (b"ESXUPDT.V00;1", 24, b"updater"),
                  (b"IMGDB.TGZ;1", 25, b"database"), (b"IMGPAYLD.TGZ;1", 26, b"payload")]
    rows = b"".join(record(block, len(content), name) for name, block, content in root_files)
    rows += record(30, 2048, b"UPGRADE", True)
    data[20 * 2048:20 * 2048 + len(rows)] = rows
    for _, block, content in root_files:
        data[block * 2048:block * 2048 + len(content)] = content
    upgrade_files = [
        (b"ESXIMAGE.ZIP;1", 31, b"PK\x03\x04esximage"),
        (b"METADATA.XML;1", 32, f"<vum><product><build>{build}</build><esxVersion>8.0.3</esxVersion></product></vum>".encode()),
        (b"METADATA.ZIP;1", 33, b"PK\x03\x04metadata"),
        (b"PRECHECK.PY;1", 34, b"# precheck"),
        (b"PREP.PY;1", 35, b"# prep"),
        (b"PROFILE.XML;1", 36, f"<imageprofile><name>ESXi-8.0U3e-{build}-standard</name></imageprofile>".encode()),
    ]
    upgrade_rows = b"".join(record(block, len(content), name) for name, block, content in upgrade_files)
    data[30 * 2048:30 * 2048 + len(upgrade_rows)] = upgrade_rows
    for _, block, content in upgrade_files:
        data[block * 2048:block * 2048 + len(content)] = content
    # Inspector deliberately rejects tiny files, so include a real-size volume tail.
    data.extend(b"\x00" * (64 * 1024 - len(data)))
    return bytes(data)


def context():
    return {"host": {"uuid": "11111111-1111-1111-1111-111111111111", "version": "7.0.3", "build": "24585291",
                     "endpoint_kind": "esxi", "maintenance_mode": False, "connection_state": "connected", "name": "demo-host",
                     "services": [{"key": "TSM-SSH", "running": True, "policy": "off"}]},
            "autostart_enabled": False, "managed_by_vcenter": False, "vsan_enabled": False,
            "vms": [{"uuid": "vm-a", "name": "Database", "vmx": "[demo] a/a.vmx", "power_state": "POWERED_ON", "tools_running": True, "template": False},
                    {"uuid": "vm-b", "name": "Application", "vmx": "[demo] b/b.vmx", "power_state": "POWERED_ON", "tools_running": True, "template": False},
                    {"uuid": "vm-off", "name": "Stopped", "vmx": "[demo] off/off.vmx", "power_state": "POWERED_OFF", "tools_running": False, "template": False}],
            "datastores": [{"uuid": "ds-demo", "name": "demo", "accessible": True}]}


class FakeSsh:
    def __init__(self, state):
        self.state = state
        self.ids = {"vm-a": "1", "vm-b": "2", "vm-off": "3"}
        self.calls = []
        self.shutdown_works = True
        self.fail_after_shutdown = False

    def identity(self):
        return self.state["host"].copy()

    def inventory(self):
        return [dict(v, id=self.ids[v["uuid"]]) for v in self.state["vms"]]

    def vm(self, ident):
        return next(v for v in self.state["vms"] if self.ids[v["uuid"]] == ident)

    def power_state(self, ident):
        return self.vm(ident)["power_state"]

    def shutdown(self, ident):
        self.calls.append(("shutdown", self.vm(ident)["uuid"]))
        if self.shutdown_works:
            self.vm(ident)["power_state"] = "POWERED_OFF"
        if self.fail_after_shutdown:
            self.fail_after_shutdown = False
            raise TransientError("lost reply")

    def power_on(self, ident):
        self.calls.append(("start", self.vm(ident)["uuid"]))
        self.vm(ident)["power_state"] = "POWERED_ON"

    def maintenance(self, enabled):
        self.calls.append(("maintenance", enabled))
        self.state["host"]["maintenance_mode"] = enabled

    def backup(self, path):
        self.calls.append(("backup",))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test-only-backup")
        return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def scenario(tmp_path):
    store = LocalStore(tmp_path / "vfleet.db")
    data = iso_bytes()
    stage = store.create_staging("installer.iso", len(data))
    store.write_staging_chunk(stage.id, 0, data)
    state = context()
    ssh = FakeSsh(state)
    service_calls = []
    def host_service_action(key, action, policy):
        service_calls.append((key, action))
        next(item for item in state["host"]["services"] if item["key"] == key)["running"] = action != "stop"
    def host_upgrade_context():
        result = copy.deepcopy(state)
        for vm in result["vms"]:
            vm["id"] = ssh.ids[vm["uuid"]]
        return result
    def start_vm_power(ident, *, power_on):
        if power_on:
            ssh.power_on(ident)
        else:
            ssh.vm(ident)["power_state"] = "POWERED_OFF"
        return ""
    def start_host_action(action, timeout_seconds=900):
        ssh.maintenance(action == "maintenance_enter")
        return ""
    adapter = SimpleNamespace(
        host_upgrade_context=host_upgrade_context,
        host_service_action=host_service_action,
        service_calls=service_calls,
        vm_power_state=ssh.power_state,
        request_guest_shutdown=ssh.shutdown,
        start_vm_power=start_vm_power,
        start_host_action=start_host_action,
        wait_task=lambda _ident: None,
    )
    settings = Settings(_env_file=None, data_dir=tmp_path, esxi_ssh_enabled=True)
    job = SimpleNamespace(id=str(uuid4()), payload={"staging_id": stage.id, "_endpoint_fingerprint": "demo-endpoint"})
    plan = inspect_plan(adapter, store, settings, job, ssh)["plan"]
    spec = UpgradePrepareRequest(plan_id=plan["id"], confirm=True, path_verified=True, hardware_verified=True,
                                vm_backups_verified=True, independent_controller=True, installer_ready=True,
                                manage_ssh_service=True,
                                publisher_sha256=plan["media"]["sha256"], startup_order=["vm-a", "vm-b"], startup_delay_seconds=0)
    prepare_job = store.enqueue("host_upgrade_prepare", "prepare", dict(spec.model_dump(mode="json"), _endpoint_fingerprint="demo-endpoint"))
    store.claim_next()
    yield SimpleNamespace(store=store, state=state, adapter=adapter, settings=settings, ssh=ssh, plan=plan, spec=spec, job=prepare_job)
    store.close()


def prepare(s):
    result = prepare_host(s.adapter, s.store, s.settings, s.job, ssh=s.ssh, sleep=lambda _: None)
    s.store.complete(s.job.id, result)
    return result


def recover(s, mode="complete"):
    job = SimpleNamespace(payload={"plan_id": s.plan["id"], "mode": mode, "confirm": True})
    return recover_host(s.adapter, s.store, s.settings, job, ssh=s.ssh, sleep=lambda _: None)


def test_iso_identity_not_filename(tmp_path):
    path = tmp_path / "misleading-7.0.iso"
    path.write_bytes(iso_bytes())
    inspected = inspect_esxi_iso(path)
    assert inspected["build"] == "24677879"
    assert inspected["installer_complete"] is True
    assert inspected["boot_module_count"] == 4
    assert inspected["image_profile"] == "ESXi-8.0U3e-24677879-standard"
    path.write_bytes(b"not an iso" * 10000)
    with pytest.raises(PermanentError):
        inspect_esxi_iso(path)


def test_iso_missing_upgrade_component_is_rejected(tmp_path):
    path = tmp_path / "incomplete.iso"
    path.write_bytes(iso_bytes().replace(b"PRECHECK.PY;1", b"NO_CHECK.PY;1"))
    with pytest.raises(PermanentError, match="UPGRADE files"):
        inspect_esxi_iso(path)


def test_out_of_bounds_iso_extent(tmp_path):
    data = bytearray(iso_bytes())
    for fmt, offset in [("<I", 2), (">I", 6)]:
        struct.pack_into(fmt, data, 20 * 2048 + offset, 9999999)
    path = tmp_path / "bad.iso"
    path.write_bytes(data)
    with pytest.raises(PermanentError, match="out-of-bounds"):
        inspect_esxi_iso(path)


def test_preflight_is_read_only(scenario):
    assert not scenario.plan["blockers"]
    assert scenario.ssh.calls == []
    assert scenario.state["vms"][0]["power_state"] == "POWERED_ON"


def test_prepare_and_restore_changed_numeric_ids(scenario):
    s = scenario
    assert prepare(s)["phase"] == "awaiting_installation"
    assert s.ssh.calls == [("backup",), ("shutdown", "vm-b"), ("shutdown", "vm-a"), ("maintenance", True)]
    s.state["host"].update(version="8.0.3", build="24677879")
    s.ssh.ids = {"vm-a": "101", "vm-b": "102", "vm-off": "103"}
    assert recover(s)["phase"] == "complete"
    assert s.ssh.calls[-3:] == [("maintenance", False), ("start", "vm-a"), ("start", "vm-b")]
    assert s.state["vms"][2]["power_state"] == "POWERED_OFF"
    assert not s.store.upgrade_lock("demo-endpoint")


def test_wrong_target_stays_in_maintenance(scenario):
    s = scenario
    prepare(s)
    before = s.ssh.calls.copy()
    with pytest.raises(PermanentError, match="version/build"):
        recover(s)
    assert s.ssh.calls == before
    assert s.state["host"]["maintenance_mode"]
    assert s.store.upgrade_lock("demo-endpoint")


def test_abort_restores_original_without_upgrade(scenario):
    s = scenario
    prepare(s)
    assert recover(s, "abort")["phase"] == "aborted"
    assert s.state["host"]["build"] == "24585291"


def test_lost_shutdown_reply_not_reissued(scenario):
    s = scenario
    s.ssh.fail_after_shutdown = True
    with pytest.raises(TransientError):
        prepare(s)
    prepare(s)
    assert s.ssh.calls.count(("shutdown", "vm-b")) == 1
    assert s.ssh.calls.count(("backup",)) == 1


def test_shutdown_timeout_never_forces_power_off(scenario):
    s = scenario
    s.ssh.shutdown_works = False
    ticks = [s.plan["created_epoch"]]
    def sleep(_):
        ticks[0] += 700
    with pytest.raises(PermanentError, match="timed out"):
        prepare_host(s.adapter, s.store, s.settings, s.job, ssh=s.ssh, now=lambda: ticks[0], sleep=sleep)
    assert not s.state["host"]["maintenance_mode"]
    assert not any(c[0] == "start" for c in s.ssh.calls)
    assert s.ssh.calls.count(("shutdown", "vm-b")) == 1


def test_missing_backup_blocks_shutdown(scenario):
    s = scenario
    def bad_backup(path):
        return "invalid"
    s.ssh.backup = bad_backup
    with pytest.raises(PermanentError, match="backup"):
        prepare(s)
    assert not s.ssh.calls


def test_media_changed_blocks_preparation(scenario):
    s = scenario
    s.store.staging_path(s.plan["staging_id"]).write_bytes(iso_bytes("24677878"))
    with pytest.raises(PermanentError, match="media changed"):
        prepare(s)
    assert not s.ssh.calls


@pytest.mark.parametrize("field", ["confirm", "path_verified", "hardware_verified", "vm_backups_verified", "independent_controller", "installer_ready", "manage_ssh_service"])
def test_prepare_requires_all_checks(scenario, field):
    spec = scenario.spec.model_copy(update={field: False})
    with pytest.raises(PermanentError):
        validate_prepare(scenario.plan, spec)


def test_shutdown_state_drift_blocks(scenario):
    s = scenario
    s.state["vms"][0]["power_state"] = "POWERED_OFF"
    with pytest.raises(PermanentError, match="configuration changed"):
        prepare(s)
    assert not s.ssh.calls


def test_missing_datastore_blocks_restore(scenario):
    s = scenario
    prepare(s)
    s.state["host"].update(version="8.0.3", build="24677879")
    s.state["datastores"][0]["accessible"] = False
    with pytest.raises(PermanentError, match="datastores"):
        recover(s)
    assert s.state["host"]["maintenance_mode"]


def test_inventory_change_blocks_restore(scenario):
    s = scenario
    prepare(s)
    s.state["host"].update(version="8.0.3", build="24677879")
    s.state["vms"][0]["vmx"] = "[other] replacement.vmx"
    with pytest.raises(PermanentError, match="configuration path"):
        recover(s)
    assert s.state["host"]["maintenance_mode"]


def test_host_reservation_blocks_other_jobs(scenario):
    s = scenario
    with pytest.raises(PermanentError, match="owns this host"):
        s.store.enqueue("power", "power", {"_endpoint_fingerprint": "demo-endpoint"})
    other = s.store.enqueue("power", "other", {"_endpoint_fingerprint": "other-endpoint"})
    assert other.status == "queued"
    s.store.fail(s.job.id, "interrupted")
    assert s.store.upgrade_lock("demo-endpoint") == s.plan["id"]


def test_unsupported_u3w_source_blocked(scenario):
    s = scenario
    s.state["host"]["build"] = "24784741"
    job = SimpleNamespace(id=str(uuid4()), payload={"staging_id": s.plan["staging_id"]})
    result = inspect_plan(s.adapter, s.store, s.settings, job, s.ssh)["plan"]
    assert any("U3w" in b for b in result["blockers"])


def test_bad_checksum_and_order_rejected(scenario):
    for update in [{"publisher_sha256": "0" * 64}, {"startup_order": ["vm-a", "vm-a"]}]:
        with pytest.raises(PermanentError):
            validate_prepare(scenario.plan, scenario.spec.model_copy(update=update))


def test_abort_does_not_release_unresolved_shutdown(scenario):
    s = scenario
    s.ssh.shutdown_works = False
    s.ssh.fail_after_shutdown = True
    with pytest.raises(TransientError):
        prepare(s)
    with pytest.raises(PermanentError, match="still unresolved"):
        recover(s, "abort")
    assert s.store.upgrade_lock("demo-endpoint")


def test_terminal_recovery_releases_orphaned_reservation(scenario):
    s = scenario
    run = load_run(s.store, s.plan["id"])
    run["phase"] = "aborted"
    save_run(s.store, run)
    assert recover(s, "abort")["phase"] == "aborted"
    assert not s.store.upgrade_lock("demo-endpoint")


def test_api_requires_confirmation_and_endpoint_ownership():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        plan_id = str(uuid4())
        run = {"id": plan_id, "endpoint_fingerprint": "another-endpoint"}
        save_run(app.state.store, run)
        response = client.get(f"/api/host/upgrade/plans/{plan_id}")
        assert response.status_code == 404
        response = client.post("/api/host/upgrade/recover", json={"plan_id": plan_id, "confirm": False})
        assert response.status_code == 404
        fingerprint = app.state.adapter.connection().endpoint_fingerprint
        run["endpoint_fingerprint"] = fingerprint
        save_run(app.state.store, run)
        response = client.post("/api/host/upgrade/recover", json={"plan_id": plan_id, "confirm": False})
        assert response.status_code == 400
        response = client.post("/api/host/upgrade/inspect", json={"staging_id": "../../.env"})
        assert response.status_code == 422


def test_new_upgrade_refused_when_endpoint_has_pending_work(tmp_path):
    store = LocalStore(tmp_path / "vfleet.db")
    try:
        store.enqueue("power", "power", {"_endpoint_fingerprint": "endpoint"})
        with pytest.raises(PermanentError, match="other endpoint jobs"):
            store.enqueue("host_upgrade_prepare", "prepare", {"_endpoint_fingerprint": "endpoint", "plan_id": str(uuid4())})
        assert not store.upgrade_lock("endpoint")
    finally:
        store.close()


def test_ssh_identity_uses_hardware_not_installation_uuid():
    from app.upgrade_ssh import UpgradeSsh
    ssh = UpgradeSsh(Settings(_env_file=None))
    responses = {("esxcfg-info", "-u"): "11111111-1111-1111-1111-111111111111",
                 ("vmware", "-vl"): "VMware ESXi 7.0.3 build-23307199\nVMware ESXi 7.0 Update 3",
                 ("esxcli", "system", "maintenanceMode", "get"): "Disabled"}
    ssh.text = lambda argv, timeout=90: responses[tuple(argv)]
    assert ssh.identity()["build"] == "23307199"
    assert ssh.identity()["uuid"] == responses[("esxcfg-info", "-u")]


def test_reservation_allows_only_confirmed_ssh_start_for_recovery(scenario):
    s = scenario
    for action in ["stop", "restart"]:
        with pytest.raises(PermanentError):
            s.store.enqueue("host_service", action, {"_endpoint_fingerprint": "demo-endpoint", "service_key": "TSM-SSH", "action": action, "confirm": True})
    with pytest.raises(PermanentError):
        s.store.enqueue("host_service", "start", {"_endpoint_fingerprint": "demo-endpoint", "service_key": "TSM-SSH", "action": "start", "confirm": False})
    job = s.store.enqueue("host_service", "start", {"_endpoint_fingerprint": "demo-endpoint", "service_key": "TSM-SSH", "action": "start", "confirm": True})
    assert job.status == "queued"
    assert s.store.upgrade_lock("demo-endpoint") == s.plan["id"]


def test_upgrade_manages_ssh_service_lifecycle(scenario):
    s = scenario
    s.state["host"]["services"][0]["running"] = False
    run = load_run(s.store, s.plan["id"])
    run["context"]["host"]["services"][0]["running"] = False
    save_run(s.store, run)
    s.plan = run
    prepare(s)
    assert s.adapter.service_calls == [("TSM-SSH", "start"), ("TSM-SSH", "stop")]
    assert s.state["host"]["services"][0]["running"] is False
    s.state["host"].update(version="8.0.3", build="24677879")
    assert recover(s)["phase"] == "complete"
    assert s.adapter.service_calls == [("TSM-SSH", "start"), ("TSM-SSH", "stop")]
    assert s.state["host"]["services"][0]["running"] is False
