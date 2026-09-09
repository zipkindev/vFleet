from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .errors import PermanentError, TransientError
from .upgrade_media import inspect_esxi_iso
from .upgrade_ssh import UpgradeSsh

SOURCES = [
    {"title": "ESXi 7.x to 8.x upgrade procedure", "url": "https://knowledge.broadcom.com/external/article/390293"},
    {"title": "Exact upgrade paths", "url": "https://interopmatrix.broadcom.com/Upgrade?productId=1"},
    {"title": "Hardware compatibility", "url": "https://compatibilityguide.broadcom.com/"},
    {"title": "Free U3e limitations", "url": "https://knowledge.broadcom.com/external/article/399823"},
    {"title": "7.0 U3w requires 8.0 U3g or later", "url": "https://knowledge.broadcom.com/external/article/418131"},
    {"title": "Backup and restore requirements", "url": "https://knowledge.broadcom.com/external/article/313510"},
    {"title": "Offline ZIP upgrade and dry-run", "url": "https://knowledge.broadcom.com/external/article/343840"},
]


class UpgradeInspectRequest(BaseModel):
    staging_id: UUID


class UpgradePrepareRequest(BaseModel):
    plan_id: UUID
    confirm: bool = False
    path_verified: bool = False
    hardware_verified: bool = False
    vm_backups_verified: bool = False
    independent_controller: bool = False
    installer_ready: bool = False
    manage_ssh_service: bool = False
    publisher_sha256: str = Field(default="", max_length=64)
    startup_order: list[str] = Field(default_factory=list, max_length=1000)
    shutdown_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    startup_delay_seconds: int = Field(default=10, ge=0, le=300)


class UpgradeRecoverRequest(BaseModel):
    plan_id: UUID
    mode: Literal["complete", "abort"] = "complete"
    confirm: bool = False


def load_run(store, ident: str) -> dict:
    ident = str(UUID(ident))
    row = store._get_kv("host_upgrade:run:" + ident)
    if row is None:
        raise PermanentError("Upgrade plan was not found")
    return json.loads(row["value"])


def save_run(store, run: dict) -> None:
    store._put_kv("host_upgrade:run:" + run["id"], json.dumps(run))


def ssh_service_running(context: dict) -> bool | None:
    for service in context.get("host", {}).get("services", []):
        if service.get("key") == "TSM-SSH":
            return bool(service.get("running"))
    return None


def ensure_ssh_service(adapter, store, run: dict, sleep=time.sleep) -> None:
    running = ssh_service_running(adapter.host_upgrade_context())
    if running is not False:
        return
    if not run.get("manage_ssh_service"):
        raise PermanentError("Start the ESXi SSH service or authorize vFleet to manage it for this upgrade")
    run["ssh_start_requested"] = True
    save_run(store, run)
    # Starting an already-running service is safe after a lost reply. Recheck
    # live state before every retry rather than guessing at the prior result.
    adapter.host_service_action("TSM-SSH", "start", "")
    for _ in range(30):
        if ssh_service_running(adapter.host_upgrade_context()) is True:
            run["ssh_started_by_upgrade"] = True
            save_run(store, run)
            return
        sleep(1)
    raise TransientError("ESXi SSH service did not become ready")


def restore_ssh_service(adapter, store, run: dict, sleep=time.sleep) -> None:
    if not run.get("manage_ssh_service"):
        return
    if ssh_service_running(adapter.host_upgrade_context()) is not True:
        return
    run["ssh_stop_requested"] = True
    save_run(store, run)
    adapter.host_service_action("TSM-SSH", "stop", "")
    for _ in range(30):
        if ssh_service_running(adapter.host_upgrade_context()) is False:
            run["ssh_stopped_after_upgrade"] = True
            save_run(store, run)
            return
        sleep(1)
    raise TransientError("ESXi SSH service did not stop after recovery")


def media_path(store, ident: str) -> Path:
    ident = str(UUID(ident))
    session = store.staging(ident)
    if session is None or not session.complete or session.received != session.size:
        raise PermanentError("Complete the installer upload before inspection")
    if not session.filename.lower().endswith(".iso"):
        raise PermanentError("Select the original ESXi installer ISO; extracted modules are not upgrade depots")
    path = store.staging_path(ident)
    if not path.is_file() or path.stat().st_size != session.size:
        raise PermanentError("Staged installer size does not match the upload")
    return path


def fingerprint(context: dict) -> dict:
    return {"uuid": context["host"]["uuid"].lower(), "version": context["host"]["version"],
            "build": context["host"]["build"],
            "vms": sorted((v["uuid"], v["vmx"], v["power_state"]) for v in context["vms"])}


def check_identity(live: dict, expected: dict) -> None:
    if any(str(live.get(k, "")).lower() != str(expected.get(k, "")).lower() for k in ("uuid", "version", "build")):
        raise PermanentError("Host UUID or running version/build differs from the reviewed upgrade plan")


def match_vms(original: list[dict], live: list[dict]) -> dict:
    before, after = {v["uuid"]: v for v in original}, {v["uuid"]: v for v in live}
    if len(before) != len(original) or len(after) != len(live) or set(before) != set(after):
        raise PermanentError("VM registrations changed; automatic restoration stopped")
    for uuid, vm in before.items():
        if vm["vmx"] != after[uuid]["vmx"]:
            raise PermanentError("A VM configuration path changed; automatic restoration stopped")
    return after


def inspect_plan(adapter, store, settings, job, ssh=None) -> dict:
    spec = UpgradeInspectRequest.model_validate(job.payload)
    media = inspect_esxi_iso(media_path(store, str(spec.staging_id)))
    context = adapter.host_upgrade_context()
    host = context["host"]
    blockers = []
    if host["endpoint_kind"] != "esxi" or context["managed_by_vcenter"] or context["vsan_enabled"]:
        blockers.append("This workflow requires a standalone ESXi host without vCenter management or vSAN.")
    if context["autostart_enabled"]:
        blockers.append("Disable host automatic VM startup before planning so vFleet can preserve power states.")
    if host["maintenance_mode"]:
        blockers.append("Host is already in maintenance mode; resolve its existing maintenance work first.")
    if host["connection_state"] != "connected" or not host["uuid"]:
        blockers.append("A connected host with a stable hardware UUID is required.")
    if media["version"] != "8.0.3" or media["build"] != "24677879":
        blockers.append("Execution currently supports the reviewed free ESXi 8.0 U3e build 24677879 ISO only.")
    if host["version"] != "7.0.3" or not str(host["build"]).isdigit():
        blockers.append("Execution currently requires an identified ESXi 7.0.3 source build.")
    elif int(host["build"]) >= 24784741:
        blockers.append("7.0 U3w and later are not approved for this U3e target; U3w requires 8.0 U3g or later.")
    for vm in context["vms"]:
        if vm["power_state"] not in {"POWERED_OFF", "POWERED_ON"}:
            blockers.append(f"{vm['name']}: resolve suspended or unknown power state before upgrading.")
        if vm["power_state"] == "POWERED_ON" and (not vm["tools_running"] or vm["template"]):
            blockers.append(f"{vm['name']}: VMware Tools must support graceful guest shutdown.")
    if not context["datastores"] or any(not ds["accessible"] for ds in context["datastores"]):
        blockers.append("All existing datastores must be accessible.")
    ssh_verified = False
    if not settings.esxi_ssh_enabled:
        blockers.append("Configure verified SSH and start the SSH service before running a live preflight.")
    else:
        try:
            ssh = ssh or UpgradeSsh(settings)
            check_identity(ssh.identity(), host)
            rows = match_vms(context["vms"], ssh.inventory())
            if any(rows[v["uuid"]]["power_state"] != v["power_state"] for v in context["vms"]):
                raise PermanentError("VM power states changed during inspection")
            ssh_verified = True
        except Exception as exc:
            detail = str(exc) if isinstance(exc, PermanentError) else type(exc).__name__
            blockers.append("Verified SSH preflight failed: " + detail)
    run = {"id": job.id, "endpoint_fingerprint": job.payload.get("_endpoint_fingerprint", ""),
           "created_at": datetime.now(timezone.utc).isoformat(), "created_epoch": time.time(),
           "phase": "planned", "media": media, "staging_id": str(spec.staging_id), "context": context,
           "blockers": blockers, "ssh_verified": ssh_verified, "sources": SOURCES,
           "shutdown_requested": [], "shutdown_completed": [], "restored": [],
           "notice": "The complete installer payload and embedded upgrade profile were verified. ISO preflight does not execute the installer compatibility scan. Exact upgrade path, hardware/driver support, and publisher checksum still require verification. U3e is the free release selected for this host, not the latest commercial ESXi release."}
    save_run(store, run)
    return {"plan": run}


def validate_prepare(run: dict, spec: UpgradePrepareRequest) -> None:
    if run["blockers"] or not run["ssh_verified"]:
        raise PermanentError("Resolve all preflight blockers and inspect again")
    if not all([spec.confirm, spec.path_verified, spec.hardware_verified, spec.vm_backups_verified,
                spec.independent_controller, spec.installer_ready, spec.manage_ssh_service]):
        raise PermanentError("Review the exact upgrade path, hardware, VM backups, independent controller, and boot media; confirm preparation")
    if spec.publisher_sha256.lower() != run["media"]["sha256"]:
        raise PermanentError("Publisher SHA-256 does not match the uploaded ISO")
    expected = {v["uuid"] for v in run["context"]["vms"] if v["power_state"] == "POWERED_ON"}
    if len(spec.startup_order) != len(expected) or set(spec.startup_order) != expected:
        raise PermanentError("Startup order must include each previously running VM exactly once")


def prepare_host(adapter, store, settings, job, ssh=None, now=time.time, sleep=time.sleep) -> dict:
    spec = UpgradePrepareRequest.model_validate(job.payload)
    run = load_run(store, str(spec.plan_id))
    validate_prepare(run, spec)
    if run["phase"] == "awaiting_installation":
        return {"plan_id": run["id"], "phase": run["phase"]}
    if run["phase"] not in {"planned", "backing_up", "shutting_down", "entering_maintenance"}:
        raise PermanentError("This upgrade cannot be prepared in its current phase")
    if run["phase"] == "planned":
        if now() - run["created_epoch"] > 900:
            raise PermanentError("Upgrade plan expired; release it using Abort/restore and inspect again")
        current = adapter.host_upgrade_context()
        if fingerprint(current) != fingerprint(run["context"]) or current["autostart_enabled"] or current["managed_by_vcenter"] or current["vsan_enabled"]:
            raise PermanentError("Host configuration changed; release the plan and inspect again")
        if current["host"]["maintenance_mode"]:
            raise PermanentError("Host entered maintenance mode outside this workflow")
        if inspect_esxi_iso(media_path(store, run["staging_id"])) != run["media"]:
            raise PermanentError("Installer media changed since inspection")
        run.update(phase="backing_up", startup_order=spec.startup_order,
                   startup_delay_seconds=spec.startup_delay_seconds,
                   manage_ssh_service=spec.manage_ssh_service)
        save_run(store, run)
    backup = store.staging_dir.parent / "upgrade-backups" / run["id"] / "host-config.tgz"
    if run["phase"] == "backing_up":
        if ssh is None:
            gate = lambda: ensure_ssh_service(adapter, store, run, sleep)
            with UpgradeSsh(settings, before_connect=gate, sleep=sleep) as session:
                return prepare_host(adapter, store, settings, job, ssh=session, now=now, sleep=sleep)
        ensure_ssh_service(adapter, store, run, sleep)
        check_identity(ssh.identity(), run["context"]["host"])
        run["backup_sha256"] = ssh.backup(backup)
        run["phase"] = "shutting_down"
        save_run(store, run)
        restore_ssh_service(adapter, store, run, sleep)
    if not backup.is_file() or hashlib.sha256(backup.read_bytes()).hexdigest() != run.get("backup_sha256"):
        raise PermanentError("The off-host configuration backup is missing or damaged")
    if run["phase"] == "shutting_down":
        rows = match_vms(run["context"]["vms"], adapter.host_upgrade_context()["vms"])
        for uuid in reversed(run["startup_order"]):
            vm = rows[uuid]
            if uuid not in run["shutdown_requested"]:
                if adapter.vm_power_state(vm["id"]) != "POWERED_ON":
                    raise PermanentError("A VM power state changed outside the upgrade; review before continuing")
                # Persist intent before issuing a command; never resend an uncertain shutdown.
                run["shutdown_requested"].append(uuid)
                run.setdefault("shutdown_deadlines", {})[uuid] = now() + spec.shutdown_timeout_seconds
                save_run(store, run)
            # SOAP guest shutdown is the primary path. This separate marker
            # also safely migrates plans paused by the former SSH implementation.
            if adapter.vm_power_state(vm["id"]) == "POWERED_ON" and uuid not in run.setdefault("soap_shutdown_requested", []):
                run["soap_shutdown_requested"].append(uuid)
                run.setdefault("shutdown_deadlines", {})[uuid] = now() + spec.shutdown_timeout_seconds
                save_run(store, run)
                adapter.request_guest_shutdown(vm["id"])
            while adapter.vm_power_state(vm["id"]) != "POWERED_OFF":
                if now() >= run["shutdown_deadlines"][uuid]:
                    raise PermanentError("Guest shutdown timed out. No forced power-off was attempted. Use Abort/restore or resolve the guest and retry.")
                sleep(2)
            if uuid not in run["shutdown_completed"]:
                run["shutdown_completed"].append(uuid)
                save_run(store, run)
                store.save_progress(job.id, {"phase": "shutting_down", "stopped": len(run["shutdown_completed"])})
        run["phase"] = "entering_maintenance"
        save_run(store, run)
    live = adapter.host_upgrade_context()
    rows = match_vms(run["context"]["vms"], live["vms"])
    if any(v["power_state"] != "POWERED_OFF" for v in rows.values()):
        raise PermanentError("All VMs must be powered off before entering maintenance mode")
    run["maintenance_requested"] = True
    save_run(store, run)
    if not live["host"]["maintenance_mode"]:
        task_id = adapter.start_host_action("maintenance_enter", timeout_seconds=120)
        if task_id:
            adapter.wait_task(task_id)
    live = adapter.host_upgrade_context()
    check_identity(live["host"], run["context"]["host"])
    if not live["host"]["maintenance_mode"]:
        raise PermanentError("ESXi did not enter maintenance mode")
    run["phase"] = "awaiting_installation"
    save_run(store, run)
    return {"plan_id": run["id"], "phase": run["phase"],
            "message": "Ready for manual ISO boot. Select Upgrade ESXi, preserve VMFS datastore on the verified existing boot device. Stop if upgrade is unavailable; do not select Install. After booting the upgraded host, return to Hosts and verify/restore."}


def recover_host(adapter, store, settings, job, ssh=None, sleep=time.sleep) -> dict:
    spec = UpgradeRecoverRequest.model_validate(job.payload)
    if not spec.confirm:
        raise PermanentError("Confirm host verification and VM restoration")
    run = load_run(store, str(spec.plan_id))
    if run["phase"] in {"complete", "aborted"}:
        store.release_upgrade(run["endpoint_fingerprint"], run["id"])
        return {"plan_id": run["id"], "phase": run["phase"]}
    if spec.mode == "complete" and run["phase"] not in {"awaiting_installation", "restoring"}:
        raise PermanentError("Host has not completed upgrade preparation")
    if run.get("recovery_mode") and run["recovery_mode"] != spec.mode:
        raise PermanentError("Cannot change an in-progress recovery mode")
    expected = dict(run["context"]["host"])
    if spec.mode == "complete":
        expected.update(version=run["media"]["version"], build=run["media"]["build"])
    live = adapter.host_upgrade_context()
    check_identity(live["host"], expected)
    if live["autostart_enabled"] or live["managed_by_vcenter"] or live["vsan_enabled"]:
        raise PermanentError("Host management or autostart configuration changed; review recovery manually")
    before_ds = {d["uuid"] for d in run["context"]["datastores"]}
    after_ds = {d["uuid"] for d in live["datastores"] if d["accessible"]}
    if not before_ds.issubset(after_ds):
        raise PermanentError("Original datastores are missing or inaccessible; host remains under upgrade control")
    rows = match_vms(run["context"]["vms"], live["vms"])
    for vm in run["context"]["vms"]:
        if vm["power_state"] == "POWERED_OFF" and rows[vm["uuid"]]["power_state"] != "POWERED_OFF":
            raise PermanentError("A previously stopped VM was started externally; review recovery manually")
    for uuid in run["shutdown_requested"]:
        if uuid not in run["shutdown_completed"]:
            if rows[uuid]["power_state"] != "POWERED_OFF":
                raise PermanentError("A guest shutdown is still unresolved; wait for it to finish before restoring or releasing the host")
            run["shutdown_completed"].append(uuid)
    save_run(store, run)
    run.update(phase="restoring", recovery_mode=spec.mode)
    save_run(store, run)
    if live["host"]["maintenance_mode"]:
        if not run.get("maintenance_requested"):
            raise PermanentError("Maintenance mode was entered outside this workflow; review manually")
        task_id = adapter.start_host_action("maintenance_exit", timeout_seconds=120)
        if task_id:
            adapter.wait_task(task_id)
    live = adapter.host_upgrade_context()
    if live["host"]["maintenance_mode"]:
        raise PermanentError("Host did not exit maintenance mode")
    rows = match_vms(run["context"]["vms"], live["vms"])
    # Only restore VMs whose shutdown this workflow requested. Stable UUID + VMX
    # matching permits ESXi numeric VM IDs to change across the installer reboot.
    for uuid in run.get("startup_order", []):
        if uuid not in run["shutdown_requested"]:
            continue
        vm = rows[uuid]
        state = adapter.vm_power_state(vm["id"])
        if state == "POWERED_OFF":
            if uuid in run.get("startup_requested", []):
                raise PermanentError("An earlier VM start has an uncertain result; inspect the VM before manual recovery")
            run.setdefault("startup_requested", []).append(uuid)
            save_run(store, run)
            task_id = adapter.start_vm_power(vm["id"], power_on=True)
            if task_id:
                adapter.wait_task(task_id)
            for _ in range(60):
                if adapter.vm_power_state(vm["id"]) == "POWERED_ON":
                    break
                sleep(2)
            else:
                raise PermanentError("VM startup did not complete; remaining VMs have not been started")
        elif state != "POWERED_ON":
            raise PermanentError("Unexpected VM power state during recovery")
        if uuid not in run["restored"]:
            run["restored"].append(uuid)
            save_run(store, run)
            sleep(run.get("startup_delay_seconds", 10))
    live = adapter.host_upgrade_context()
    check_identity(live["host"], expected)
    rows = match_vms(run["context"]["vms"], live["vms"])
    if any(rows[v["uuid"]]["power_state"] != v["power_state"] for v in run["context"]["vms"]):
        raise PermanentError("VM power states do not match the saved manifest; review recovery")
    restore_ssh_service(adapter, store, run, sleep)
    run["phase"] = "complete" if spec.mode == "complete" else "aborted"
    save_run(store, run)
    store.release_upgrade(run["endpoint_fingerprint"], run["id"])
    return {"plan_id": run["id"], "phase": run["phase"], "restored": len(run["restored"]),
            "message": "Host identity and original VM power states verified. Guest application health still requires operator validation."}
