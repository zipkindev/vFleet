from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, Optional

from .models import (
    Catalog,
    InventorySnapshot,
    VirtualMachine,
    VmHardwarePlan,
    VmHardwarePlanRequest,
    VmHardwareTargetPlan,
)
from .power import normalize_power_state

MIB = 1024**2
GIB = 1024**3
TOOLS_RUNNING = {"toolsok", "guesttoolsrunning"}


def normalize_hardware_request(spec: VmHardwarePlanRequest) -> VmHardwarePlanRequest:
    vm_ids = list(dict.fromkeys(item.strip() for item in spec.vm_ids if item and item.strip()))
    iso_action = spec.iso_action.strip().lower()
    iso_path = spec.iso_path.strip().strip("/")
    datastore_id = spec.iso_datastore_id.strip()
    disk_capacity = spec.disk_capacity_bytes
    if disk_capacity is not None:
        disk_capacity = int(disk_capacity // MIB * MIB)
    return spec.model_copy(
        update={
            "vm_ids": vm_ids,
            "disk_capacity_bytes": disk_capacity,
            "iso_action": iso_action,
            "iso_datastore_id": datastore_id,
            "iso_path": iso_path,
        }
    )


def validate_hardware_request(spec: VmHardwarePlanRequest) -> VmHardwarePlanRequest:
    clean = normalize_hardware_request(spec)
    if not clean.vm_ids:
        raise ValueError("No virtual machines selected")
    if len(clean.vm_ids) > 50:
        raise ValueError("Refusing more than 50 VMs in one hardware job (split into a second reviewed batch)")
    if clean.memory_mib is not None and clean.memory_mib % 4:
        raise ValueError("Memory must be specified in 4 MiB increments")
    if (clean.disk_index is None) != (clean.disk_capacity_bytes is None):
        raise ValueError("Choose both a disk number and a target disk capacity")
    if clean.disk_capacity_bytes is not None and clean.disk_capacity_bytes < GIB:
        raise ValueError("Disk capacity must be at least 1 GiB")
    if clean.force_power_off_on_timeout and not clean.shutdown_before:
        raise ValueError("Forced power-off fallback requires guest shutdown before the hardware change")
    if clean.power_on_after and not clean.shutdown_before:
        raise ValueError("Automatic startup requires guest shutdown before the hardware change")
    if clean.iso_action == "mount":
        if not clean.iso_datastore_id or not clean.iso_path:
            raise ValueError("Choose a datastore and ISO path to mount")
        if not clean.iso_path.lower().endswith(".iso"):
            raise ValueError("ISO path must end in .iso")
        if any(part in {".", ".."} for part in clean.iso_path.split("/")):
            raise ValueError("ISO path cannot contain '.' or '..' path segments")
    elif clean.iso_action == "eject":
        clean = clean.model_copy(update={"iso_datastore_id": "", "iso_path": ""})
    else:
        clean = clean.model_copy(
            update={"iso_action": "keep", "iso_datastore_id": "", "iso_path": ""}
        )
    if (
        clean.cpu_count is None
        and clean.memory_mib is None
        and clean.disk_capacity_bytes is None
        and clean.iso_action == "keep"
    ):
        raise ValueError("Choose at least one CPU, memory, disk, or ISO change")
    return clean


def _datastore_index(catalog: Optional[Catalog]) -> Dict[str, object]:
    return {item.id: item for item in (catalog.datastores if catalog else [])}


def _plan_target(
    vm: VirtualMachine,
    spec: VmHardwarePlanRequest,
    datastores: Dict[str, object],
    iso_file_exists: Optional[bool],
) -> VmHardwareTargetPlan:
    row = VmHardwareTargetPlan(
        vm_id=vm.id,
        name=vm.name,
        cluster_id=vm.cluster_id,
        cluster_name=vm.cluster_name,
        host_id=vm.host_id,
        host_name=vm.host_name,
        power_state=normalize_power_state(vm.power_state),
        current_cpu_count=vm.cpu_count,
        target_cpu_count=spec.cpu_count,
        current_memory_mib=vm.memory_mib,
        target_memory_mib=spec.memory_mib,
        disk_index=spec.disk_index,
        target_disk_capacity_bytes=spec.disk_capacity_bytes,
        current_iso_path=vm.mounted_iso_path,
    )
    running = row.power_state != "POWERED_OFF"
    workflow_can_stop = False

    if spec.shutdown_before and running:
        row.will_shutdown_before = True
        tools_running = (vm.tools_status or "").strip().lower() in TOOLS_RUNNING
        if row.power_state == "POWERED_ON" and tools_running:
            workflow_can_stop = True
        elif spec.force_power_off_on_timeout:
            workflow_can_stop = True
            if row.power_state == "POWERED_ON":
                row.warnings.append(
                    "VMware Tools is not running; forced power-off may be required"
                )
            else:
                row.warnings.append(
                    f"{row.power_state} cannot perform guest shutdown; forced power-off will be used"
                )
        elif row.power_state == "POWERED_ON":
            row.blockers.append(
                "VMware Tools must be running for guest shutdown, or enable forced power-off fallback"
            )
        else:
            row.blockers.append(
                f"{row.power_state} cannot perform guest shutdown; enable forced power-off fallback or power it off first"
            )
        if workflow_can_stop:
            row.warnings.append("Guest shutdown will be requested before the hardware change")
            row.may_force_power_off = spec.force_power_off_on_timeout
            if row.may_force_power_off:
                row.warnings.append(
                    f"If shutdown does not finish within {spec.shutdown_timeout_seconds} seconds, vFleet may force power off"
                )
            row.will_power_on_after = spec.power_on_after and row.power_state == "POWERED_ON"
            if row.will_power_on_after:
                row.warnings.append("The VM will be powered back on when its workflow ends")

    if spec.cpu_count is not None and spec.cpu_count != vm.cpu_count:
        if running and spec.cpu_count < vm.cpu_count and not workflow_can_stop and not spec.shutdown_before:
            row.blockers.append("Power off the VM before reducing vCPU")
        elif running and not vm.cpu_hot_add_enabled and not workflow_can_stop and not spec.shutdown_before:
            row.blockers.append("Power off the VM or enable CPU hot-add before increasing vCPU")
        if vm.cores_per_socket > 1 and spec.cpu_count % vm.cores_per_socket:
            row.warnings.append(
                f"CPU topology will change from {vm.cores_per_socket} cores per socket to 1"
            )
        row.changes.append(f"vCPU {vm.cpu_count} → {spec.cpu_count}")

    if spec.memory_mib is not None and spec.memory_mib != vm.memory_mib:
        if running and spec.memory_mib < vm.memory_mib and not workflow_can_stop and not spec.shutdown_before:
            row.blockers.append("Power off the VM before reducing memory")
        elif running and not vm.memory_hot_add_enabled and not workflow_can_stop and not spec.shutdown_before:
            row.blockers.append("Power off the VM or enable memory hot-add before increasing memory")
        row.changes.append(f"memory {vm.memory_mib / 1024:g} → {spec.memory_mib / 1024:g} GiB")

    if spec.disk_index is not None and spec.disk_capacity_bytes is not None:
        if spec.disk_index >= len(vm.disks):
            row.blockers.append(f"VM does not have disk {spec.disk_index + 1}")
        else:
            disk = vm.disks[spec.disk_index]
            row.disk_label = disk.label or f"Disk {spec.disk_index + 1}"
            row.current_disk_capacity_bytes = disk.capacity_bytes
            if spec.disk_capacity_bytes < disk.capacity_bytes:
                row.blockers.append(f"{row.disk_label} cannot be shrunk")
            elif spec.disk_capacity_bytes > disk.capacity_bytes:
                if disk.rdm:
                    row.blockers.append(f"{row.disk_label} is an RDM or device mapping")
                if disk.encrypted:
                    row.blockers.append(f"{row.disk_label} is encrypted")
                if disk.parent_depth:
                    row.blockers.append(f"{row.disk_label} has a snapshot/backing chain")
                if disk.sharing and disk.sharing.lower() not in {"", "sharingnone"}:
                    row.blockers.append(f"{row.disk_label} uses shared-disk mode {disk.sharing}")
                row.changes.append(
                    f"{row.disk_label} {disk.capacity_bytes / GIB:g} → {spec.disk_capacity_bytes / GIB:g} GiB"
                )
                datastore = datastores.get(disk.datastore_id)
                growth = spec.disk_capacity_bytes - disk.capacity_bytes
                free = int(getattr(datastore, "free_bytes", 0) or 0)
                if datastore is not None and free and growth > free:
                    if disk.provisioning == "thin":
                        row.warnings.append("Requested thin capacity exceeds current datastore free space")
                    else:
                        row.blockers.append("Datastore does not have enough free space for this disk growth")

    if spec.iso_action == "mount":
        if vm.tools_installer_mounted:
            row.blockers.append("VMware Tools installer media is mounted; unmount it before changing the CD/DVD ISO")
        if iso_file_exists is False:
            row.blockers.append("ISO file was not found at the reviewed datastore path")
        datastore = datastores.get(spec.iso_datastore_id)
        if datastore is None:
            row.blockers.append("ISO datastore is not present in the current catalog")
            target_path = spec.iso_path
        else:
            target_path = f"[{getattr(datastore, 'name', spec.iso_datastore_id)}] {spec.iso_path}"
            if not bool(getattr(datastore, "accessible", True)):
                row.blockers.append("ISO datastore is not currently accessible")
            if bool(getattr(datastore, "readonly", False)):
                row.warnings.append("ISO datastore is mounted read-only; mounting existing media is still allowed")
            host_ids: Iterable[str] = getattr(datastore, "host_ids", []) or []
            if vm.host_id and host_ids and vm.host_id not in host_ids:
                row.blockers.append("ISO datastore is not mounted on this VM's current host")
        row.target_iso_path = target_path
        if vm.cdrom_count < 1:
            row.blockers.append("VM has no virtual CD/DVD drive")
        elif (
            vm.mounted_iso_path != target_path
            or (row.power_state == "POWERED_ON" and not vm.iso_connected)
            or vm.iso_start_connected != spec.iso_connect_at_power_on
        ):
            row.changes.append(f"mount ISO {target_path}")
    elif spec.iso_action == "eject":
        if vm.tools_installer_mounted:
            row.blockers.append("VMware Tools installer media is mounted; use the Tools workflow to restore it")
        if vm.cdrom_count > 0 and (vm.iso_connected or vm.iso_start_connected):
            row.changes.append("disconnect mounted ISO")

    row.noop = not row.changes and not row.blockers
    row.can_execute = bool(row.changes) and not row.blockers
    return row


def build_vm_hardware_plan(
    snapshot: InventorySnapshot,
    catalog: Optional[Catalog],
    request: VmHardwarePlanRequest,
    *,
    iso_file_exists: Optional[bool] = None,
) -> VmHardwarePlan:
    validated = validate_hardware_request(request)
    spec = VmHardwarePlanRequest.model_validate(
        validated.model_dump(include=set(VmHardwarePlanRequest.model_fields))
    )
    by_id = {vm.id: vm for vm in snapshot.vms}
    datastores = _datastore_index(catalog)
    targets = []
    for vm_id in spec.vm_ids:
        vm = by_id.get(vm_id)
        if vm is None:
            targets.append(
                VmHardwareTargetPlan(
                    vm_id=vm_id,
                    name=vm_id,
                    blockers=["VM is not present in the current inventory"],
                    can_execute=False,
                )
            )
            continue
        targets.append(_plan_target(vm, spec, datastores, iso_file_exists))

    token_payload = {
        "request": spec.model_dump(),
        "targets": [row.model_dump() for row in targets],
    }
    token = hashlib.sha256(
        json.dumps(token_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return VmHardwarePlan(
        plan_token=token,
        targets=targets,
        can_execute_count=sum(1 for row in targets if row.can_execute),
        blocked_count=sum(1 for row in targets if row.blockers),
        noop_count=sum(1 for row in targets if row.noop),
    )
