from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

from .adapters.base import InventoryAdapter
from .models import (
    InventorySnapshot,
    Job,
    StorageReconciliationCandidate,
    StorageReconciliationReport,
    StorageReconciliationVmStatus,
    VirtualMachine,
)
from .store import LocalStore


_DATASTORE_PATH = re.compile(r"^\[([^\]]+)\]\s+(.+)$")
_TOOLS_RUNNING = {"toolsok", "guesttoolsrunning"}


def _split_datastore_path(value: str) -> Optional[Tuple[str, str]]:
    match = _DATASTORE_PATH.match((value or "").strip())
    if not match:
        return None
    datastore = match.group(1).strip()
    path = str(PurePosixPath(match.group(2).strip().lstrip("/")))
    if not datastore or not path or path == "." or ".." in PurePosixPath(path).parts:
        return None
    return datastore, path


def _candidate_id(kind: str, datastore_id: str, path: str, job_id: str = "") -> str:
    raw = f"{kind}\0{datastore_id}\0{path}\0{job_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_tools_running(vm: VirtualMachine) -> bool:
    return (vm.tools_status or "").strip().lower() in _TOOLS_RUNNING


def _vm_validation(vm: VirtualMachine, completed_at: Optional[datetime]) -> StorageReconciliationVmStatus:
    tools_running = _is_tools_running(vm)
    powered_on = vm.power_state == "POWERED_ON"
    boot_after_job = bool(vm.boot_time and completed_at and vm.boot_time >= completed_at)
    automated = powered_on and tools_running and boot_after_job
    if automated:
        message = "Powered on after conversion with VMware Tools running; post-boot inventory validation passed."
    elif not powered_on:
        message = "Power on the VM and validate the guest before removing preserved source storage."
    elif not tools_running:
        message = (
            "VMware Tools is not running. Deploy Tools for automated reconciliation, or manually validate the guest "
            "after boot and acknowledge that validation before cleanup."
        )
    elif not boot_after_job:
        message = "The VM has not recorded a boot after this conversion; boot and validate it before cleanup."
    else:
        message = "Manual post-boot validation is required before cleanup."
    return StorageReconciliationVmStatus(
        vm_id=vm.id,
        vm_name=vm.name,
        power_state=vm.power_state,
        tools_status=vm.tools_status,
        tools_running=tools_running,
        boot_validated=automated,
        manual_validation_required=not automated,
        message=message,
    )


def _attached_paths(snapshot: InventorySnapshot) -> tuple[set[Tuple[str, str]], Dict[Tuple[str, str], VirtualMachine]]:
    paths: set[Tuple[str, str]] = set()
    directories: Dict[Tuple[str, str], VirtualMachine] = {}
    for vm in snapshot.vms:
        for disk in vm.disks:
            parsed = _split_datastore_path(disk.file_name)
            if not parsed:
                continue
            paths.add(parsed)
            directories[(parsed[0], str(PurePosixPath(parsed[1]).parent))] = vm
    return paths, directories


def _latest_conversion_jobs(jobs: Iterable[Job], vm_ids: set[str]) -> List[Job]:
    rows: List[Job] = []
    for job in jobs:
        if job.kind != "disk_convert" or job.status != "succeeded":
            continue
        plan = job.payload.get("plan") or {}
        vm_id = str(plan.get("vm_id") or job.result.get("vm_id") or "")
        if vm_ids and vm_id not in vm_ids:
            continue
        if job.result.get("converted"):
            rows.append(job)
    return rows


def build_storage_reconciliation(
    adapter: InventoryAdapter,
    store: LocalStore,
    *,
    vm_ids: Iterable[str] = (),
    include_unregistered_directories: bool = True,
) -> StorageReconciliationReport:
    requested = {str(item).strip() for item in vm_ids if str(item).strip()}
    snapshot = adapter.snapshot()
    vm_by_id = {vm.id: vm for vm in snapshot.vms}
    unknown = sorted(requested - set(vm_by_id))
    scoped_vms = [vm for vm in snapshot.vms if not requested or vm.id in requested]
    attached, inventory_dirs = _attached_paths(snapshot)
    datastore_rows = adapter.list_datastores()
    datastore_by_name = {row.name: row for row in datastore_rows}
    candidates: List[StorageReconciliationCandidate] = []
    statuses: Dict[str, StorageReconciliationVmStatus] = {}
    warnings: List[str] = []
    scanned_directories = 0

    if snapshot.connection.stale:
        warnings.append("Inventory is stale. Reconnect and refresh before queueing any cleanup.")
    if unknown:
        warnings.append(f"VMs no longer present in inventory: {', '.join(unknown)}")

    known_sources: set[Tuple[str, str]] = set()
    jobs = _latest_conversion_jobs(
        store.list_jobs(limit=500, endpoint_fingerprint=adapter.endpoint_fingerprint()), requested
    )
    for job in jobs:
        plan = job.payload.get("plan") or {}
        vm_id = str(plan.get("vm_id") or job.result.get("vm_id") or "")
        vm = vm_by_id.get(vm_id)
        if vm is None:
            warnings.append(f"Conversion job {job.id[:8]} references VM {vm_id or 'unknown'}, which is no longer inventoried.")
            continue
        status = _vm_validation(vm, job.updated_at)
        for converted in job.result.get("converted") or []:
            parsed_source = _split_datastore_path(str(converted.get("source") or ""))
            parsed_destination = _split_datastore_path(str(converted.get("destination") or ""))
            if not parsed_source:
                continue
            datastore_name, path = parsed_source
            datastore = datastore_by_name.get(datastore_name)
            if datastore is None:
                warnings.append(f"Datastore {datastore_name} from {vm.name}'s conversion is no longer available.")
                continue
            if parsed_source in known_sources:
                continue
            known_sources.add(parsed_source)
            if parsed_source in attached:
                warnings.append(f"{datastore_name}/{path} is attached again and was excluded from cleanup.")
                continue
            if not parsed_destination or parsed_destination not in attached:
                warnings.append(
                    f"{vm.name}'s converted destination is not attached in current inventory; preserved source {path} is blocked."
                )
                continue
            size = adapter.stat_file(datastore.id, path)
            if size is None:
                continue
            statuses[vm.id] = status
            validation = "automated" if status.boot_validated else ("manual_required" if vm.power_state == "POWERED_ON" else "blocked")
            warning = "" if status.boot_validated else status.message
            candidates.append(
                StorageReconciliationCandidate(
                    id=_candidate_id("preserved_source_disk", datastore.id, path, job.id),
                    kind="preserved_source_disk",
                    datastore_id=datastore.id,
                    datastore_name=datastore.name,
                    path=path,
                    size=size,
                    vm_id=vm.id,
                    vm_name=vm.name,
                    job_id=job.id,
                    confidence="high",
                    validation_status=validation,
                    can_delete=not snapshot.connection.stale and validation == "automated",
                    reason="Preserved source VMDK from a completed conversion; replacement disk is currently attached.",
                    warning=warning,
                )
            )

    scan_dirs: Dict[Tuple[str, str], VirtualMachine] = {}
    for key, vm in inventory_dirs.items():
        if not requested or vm.id in requested:
            scan_dirs[key] = vm

    # A full reconciliation also inspects each datastore's first-level folders.
    if not requested and include_unregistered_directories:
        for datastore in datastore_rows:
            try:
                root = adapter.browse_datastore(datastore.id, "")
            except Exception as exc:
                warnings.append(f"Could not scan {datastore.name}: {exc}")
                continue
            for item in root.files:
                if not item.is_directory:
                    continue
                key = (datastore.name, item.path.strip("/"))
                if key not in inventory_dirs:
                    try:
                        listing = adapter.browse_datastore(datastore.id, item.path)
                        scanned_directories += 1
                    except Exception as exc:
                        warnings.append(f"Could not inspect {datastore.name}/{item.path}: {exc}")
                        continue
                    vm_files = [
                        child for child in listing.files
                        if child.name.lower().endswith(".vmx")
                        or child.kind == "disk"
                        or child.name.lower().endswith(".vmdk")
                    ]
                    if not vm_files:
                        continue
                    size = sum(max(0, int(child.size or 0)) for child in listing.files if not child.is_directory)
                    candidates.append(
                        StorageReconciliationCandidate(
                            id=_candidate_id("unregistered_vm_directory", datastore.id, item.path),
                            kind="unregistered_vm_directory",
                            datastore_id=datastore.id,
                            datastore_name=datastore.name,
                            path=item.path,
                            size=size,
                            confidence="review",
                            validation_status="manual_required",
                            can_delete=False,
                            reason="Folder contains VM configuration or disk files but no current VM disk points into it.",
                            warning="Confirm the VM is not intentionally unregistered or retained for recovery before deleting this directory.",
                        )
                    )

    # Inspect active VM directories for descriptor disks that are not attached.
    for (datastore_name, directory), vm in scan_dirs.items():
        datastore = datastore_by_name.get(datastore_name)
        if datastore is None:
            continue
        directory_disks = [
            disk
            for disk in vm.disks
            if (parsed := _split_datastore_path(disk.file_name))
            and parsed[0] == datastore_name
            and str(PurePosixPath(parsed[1]).parent) == directory
        ]
        if any(disk.parent_depth > 0 for disk in directory_disks):
            warnings.append(
                f"Skipped unattached-disk detection in {datastore.name}/{directory}: {vm.name} has a snapshot backing chain."
            )
            continue
        try:
            listing = adapter.browse_datastore(datastore.id, "" if directory == "." else directory)
            scanned_directories += 1
        except Exception as exc:
            warnings.append(f"Could not inspect {datastore.name}/{directory}: {exc}")
            continue
        for item in listing.files:
            path = item.path.strip("/")
            parsed = (datastore.name, path)
            lowered = item.name.lower()
            if item.is_directory or parsed in attached or parsed in known_sources:
                continue
            if item.kind != "disk" and not lowered.endswith(".vmdk"):
                continue
            if lowered.endswith(("-flat.vmdk", "-delta.vmdk", "-ctk.vmdk", "-sesparse.vmdk")):
                continue
            if re.search(r"-\d{6}\.vmdk$", lowered):
                continue
            candidates.append(
                StorageReconciliationCandidate(
                    id=_candidate_id("unattached_virtual_disk", datastore.id, path),
                    kind="unattached_virtual_disk",
                    datastore_id=datastore.id,
                    datastore_name=datastore.name,
                    path=path,
                    size=max(0, int(item.size or 0)),
                    vm_id=vm.id,
                    vm_name=vm.name,
                    confidence="review",
                    validation_status="manual_required" if vm.power_state == "POWERED_ON" else "blocked",
                    can_delete=False,
                    reason="VMDK descriptor is in an inventoried VM folder but is not attached to any inventoried VM.",
                    warning=(
                        "Review snapshots, backup retention, and guest operation before cleanup."
                        if vm.power_state == "POWERED_ON"
                        else "Power on and validate the VM before considering this disk for cleanup."
                    ),
                )
            )

    candidates.sort(key=lambda row: (row.datastore_name.lower(), row.path.lower(), row.kind))
    statuses_list = sorted(statuses.values(), key=lambda row: row.vm_name.lower())
    token_payload = {
        "endpoint": adapter.endpoint_fingerprint(),
        "stale": snapshot.connection.stale,
        "vm_ids": sorted(requested),
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
    }
    plan_token = hashlib.sha256(json.dumps(token_payload, sort_keys=True).encode("utf-8")).hexdigest()
    return StorageReconciliationReport(
        plan_token=plan_token,
        scanned_at=datetime.now(timezone.utc),
        inventory_stale=snapshot.connection.stale,
        vm_ids=sorted(requested),
        scanned_directories=scanned_directories,
        candidates=candidates,
        vm_statuses=statuses_list,
        warnings=warnings,
    )


def conversion_reconciliation(
    adapter: InventoryAdapter,
    plan_vm_id: str,
    result: Dict[str, object],
) -> Dict[str, object]:
    """Attach immediate, non-destructive reconciliation guidance to a conversion job."""
    snapshot = adapter.snapshot()
    vm = next((item for item in snapshot.vms if item.id == plan_vm_id), None)
    if vm is None:
        return {"status": "warning", "message": "VM is not present in refreshed inventory; run Storage reconciliation."}
    if not result.get("source_disks_preserved"):
        return {"status": "clean", "message": "vSphere completed the relocation without a preserved SSH source disk."}
    tools_running = _is_tools_running(vm)
    if vm.power_state != "POWERED_ON":
        message = "Source disks were preserved. Power on and validate the VM, then run Storage reconciliation."
    elif not tools_running:
        message = (
            "Source disks were preserved, but VMware Tools is not running. Deploy Tools for automated validation or "
            "manually validate the guest after boot before cleanup."
        )
    else:
        message = "Source disks were preserved. Run Storage reconciliation after the post-conversion boot is validated."
    return {
        "status": "action_required",
        "message": message,
        "vm_id": vm.id,
        "vm_name": vm.name,
        "tools_running": tools_running,
        "power_state": vm.power_state,
        "preserved_sources": [str(item.get("source") or "") for item in result.get("converted") or []],
    }
