from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from .adapters.base import InventoryAdapter
from .config import Settings
from .errors import PermanentError, humanize_vcenter_error, is_permanent, is_transient
from .models import (
    Catalog,
    CloneMigrateRequest,
    CloneVmRequest,
    ConnectionInfo,
    DrsOverrideRequest,
    DiskConversionPlan,
    DiskConversionPlanRequest,
    HostActionRequest,
    HostServiceActionRequest,
    HostTimeRequest,
    InventorySnapshot,
    Job,
    MigrateVmRequest,
    RenameVmRequest,
)
from .store import LocalStore
from .reclaim import build_owner_reports
from .automation_vault import AutomationCredentialStore


def _clone_result_id(adapter: InventoryAdapter, result, task_id: str, clone_name: str) -> str:
    if result is not None:
        getter = getattr(result, "_GetMoId", None)
        if callable(getter):
            moid = str(getter() or "")
            if moid:
                return moid
        moid = str(getattr(result, "_moId", "") or getattr(result, "id", "") or "")
        if moid:
            return moid
    if task_id.startswith("demo-clone-"):
        return task_id[len("demo-clone-") :]
    name = (clone_name or "").strip()
    if name:
        try:
            snapshot = adapter.snapshot()
        except Exception:
            snapshot = None
        if snapshot is not None:
            found = next((vm for vm in snapshot.vms if vm.name == name), None)
            if found is not None:
                return found.id
    return ""


class RelayWorker(threading.Thread):
    def __init__(
        self,
        settings: Settings,
        store: LocalStore,
        adapter_getter,
        automation_credentials: Optional[AutomationCredentialStore] = None,
    ) -> None:
        super().__init__(daemon=True, name="vfleet-relay")
        self.settings = settings
        self.store = store
        self._adapter_getter = adapter_getter
        self.automation_credentials = automation_credentials
        self._halt = threading.Event()
        self._wake = threading.Event()
        self.syncing = False
        self.last_error = ""
        self.last_sync_ok: Optional[datetime] = None
        self.reachable: Optional[bool] = None

    def adapter(self) -> InventoryAdapter:
        return self._adapter_getter()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        self.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def run(self) -> None:
        self.store.requeue_orphans()
        self.sync_now(force=True)
        while not self._halt.is_set():
            self.sync_now(force=False)
            self._heartbeat()
            self._drain()
            self._wake.wait(self.settings.relay_tick_seconds)
            self._wake.clear()

    def _record_metrics(self, adapter: InventoryAdapter, snapshot: InventorySnapshot, catalog: Optional[Catalog]) -> None:
        from .metrics import build_metric_samples

        live = build_metric_samples(snapshot, catalog)
        if not self.store.history_seeded():
            try:
                history = adapter.historical_metrics(snapshot, catalog)
                if history:
                    self.store.record_metrics(history)
            except Exception:
                history = []
            self.store.mark_history_seeded()
        self.store.record_metrics(live)

    def sync_now(self, force: bool = False) -> None:
        last = self.store.inventory_saved_at()
        if not force and last is not None:
            age = (datetime.now(timezone.utc) - last).total_seconds()
            if age < self.settings.sync_interval_seconds:
                return
        adapter = self.adapter()
        self.syncing = True
        try:
            snapshot = adapter.snapshot()
            previous = self.store.load_inventory()
            if previous is not None:
                known = {vm.id: vm.deployed_by for vm in previous.vms if vm.deployed_by}
                for vm in snapshot.vms:
                    if not vm.deployed_by and vm.id in known:
                        vm.deployed_by = known[vm.id]
                snapshot.owners = build_owner_reports(snapshot.vms)
            self.store.save_inventory(snapshot)
            catalog: Optional[Catalog] = None
            try:
                networks = []
                try:
                    networks = adapter.list_networks()
                except Exception:
                    networks = []
                catalog = Catalog(
                    datastores=adapter.list_datastores(),
                    templates=adapter.list_templates(),
                    networks=networks,
                    last_sync=datetime.now(timezone.utc),
                )
                self.store.save_catalog(catalog)
            except Exception as exc:
                self.last_error = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
            try:
                self._record_metrics(adapter, snapshot, catalog or self.store.load_catalog())
            except Exception:
                pass
            self.last_error = ""
            self.last_sync_ok = datetime.now(timezone.utc)
            self.reachable = True
        except Exception as exc:
            self.last_error = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
            self.reachable = False
        finally:
            self.syncing = False

    def overlay(self, conn: ConnectionInfo) -> ConnectionInfo:
        counts = self.store.counts()
        saved_at = self.store.inventory_saved_at()
        age = None
        if saved_at is not None:
            age = max(0.0, (datetime.now(timezone.utc) - saved_at).total_seconds())
        stale = False
        if conn.mode in {"vcenter", "esxi"}:
            if self.reachable is False:
                conn = conn.model_copy(update={"connected": False})
            # A full inventory pull of a large vCenter can exceed sync_interval.
            # Only treat the relay as stale when the session is actually down.
            stale = self.reachable is False and not self.syncing
        message = conn.message
        if stale and saved_at is not None:
            message = self.last_error or conn.message or "vCenter unreachable; showing last local snapshot"
        return conn.model_copy(
            update={
                "stale": stale,
                "syncing": self.syncing,
                "last_error": self.last_error,
                "queued_jobs": counts["queued"],
                "active_jobs": counts["active"],
                "cache_age_seconds": age,
                "last_sync": saved_at or conn.last_sync,
                "message": message,
            }
        )

    def cached_snapshot(self) -> Optional[InventorySnapshot]:
        snapshot = self.store.load_inventory()
        if snapshot is None:
            return None
        from .adapters.demo import DemoAdapter

        adapter = self.adapter()
        if isinstance(adapter, DemoAdapter):
            live = adapter.connection()
        else:
            try:
                connection = adapter.connection()
            except Exception:
                connection = snapshot.connection
            if not connection.endpoint_fingerprint and snapshot.connection.endpoint_fingerprint:
                connection = snapshot.connection.model_copy(
                    update={
                        "connected": False,
                        "message": self.last_error or connection.message or snapshot.connection.message,
                    }
                )
            live = connection.model_copy(
                update={
                    "connected": self.reachable is not False and not self.last_error,
                    "message": self.last_error or connection.message or snapshot.connection.message,
                }
            )
        snapshot.connection = self.overlay(live)
        return snapshot

    def _heartbeat(self) -> None:
        if self.syncing:
            return
        adapter = self.adapter()
        ping = getattr(adapter, "ping", None)
        if not callable(ping):
            return
        try:
            ping()
            self.reachable = True
            self.last_error = ""
        except Exception as exc:
            self.last_error = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
            self.reachable = False

    def _drain(self) -> None:
        job = self.store.claim_next()
        if job is None:
            return
        try:
            result = self._execute(job)
            try:
                self.sync_now(force=True)
            except Exception:
                pass
            self.store.complete(job.id, result)
        except Exception as exc:
            attempts = job.attempts + 1
            if is_permanent(exc) or attempts >= job.max_attempts:
                self.store.fail(job.id, str(exc))
            elif is_transient(exc) or not is_permanent(exc):
                refreshed = self.store.get(job.id)
                progress = refreshed.progress if refreshed else job.progress
                self.store.retry(job.id, str(exc), attempts, progress)
            else:
                self.store.fail(job.id, str(exc))

    def _execute(self, job: Job) -> dict:
        adapter = self.adapter()
        kind = job.kind
        payload = job.payload
        bound = str(payload.get("_endpoint_fingerprint") or "")
        if bound:
            current = adapter.endpoint_fingerprint()
            if not current or current != bound:
                raise PermanentError(
                    "This job was created for a different vSphere endpoint and will not be executed here"
                )
        progress = dict(job.progress)

        def persist(sent: int, extra: dict) -> None:
            extra = dict(extra)
            extra["bytes_sent"] = sent
            self.store.save_progress(job.id, extra)

        if kind == "power":
            results = adapter.apply_actions(payload["vm_ids"], payload["action"])
            failed = [row for row in results if not row.ok]
            if failed:
                messages = "; ".join(f"{row.name or row.vm_id}: {row.message}" for row in failed)
                if any(is_transient(Exception(row.message)) for row in failed):
                    raise Exception(messages)
                if len(failed) == len(results):
                    raise PermanentError(messages)
            return {"results": [row.model_dump() for row in results]}

        if kind in {"clone", "deploy"}:
            spec = CloneVmRequest.model_validate(payload)
            task_id = str(progress.get("task_id") or "")
            if not task_id:
                task_id = adapter.start_clone(spec)
                progress["task_id"] = task_id
                self.store.save_progress(job.id, progress)
            vm = adapter.wait_task(task_id)
            progress["clone_complete"] = True
            self.store.save_progress(job.id, progress)
            if spec.disable_drs and not progress.get("drs_disabled"):
                try:
                    drs_task = adapter.disable_vm_drs(vm, cluster_id=spec.cluster_id, vm_name=spec.name)
                    if drs_task:
                        adapter.wait_task(drs_task)
                    progress["drs_disabled"] = True
                    self.store.save_progress(job.id, progress)
                except Exception as exc:
                    detail = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
                    raise PermanentError(
                        f"VM {spec.name} was created, but DRS pin failed: {detail}"
                    ) from exc
            return {"name": spec.name, "task_id": task_id, "drs_disabled": bool(progress.get("drs_disabled"))}

        if kind == "clone_migrate":
            spec = CloneMigrateRequest.model_validate(payload)
            phase = str(progress.get("phase") or "clone")
            source_id = (spec.vm_id or "").strip()
            original_name = str(progress.get("original_name") or payload.get("original_name") or "")
            clone_name = str(progress.get("clone_name") or spec.name or "").strip()
            source_old_name = str(progress.get("source_old_name") or "")
            clone_vm_id = str(progress.get("clone_vm_id") or "")

            if phase == "clone":
                task_id = str(progress.get("task_id") or "")
                if not task_id:
                    task_id = adapter.start_clone_migrate(spec)
                    progress["task_id"] = task_id
                    progress["phase"] = "clone_wait"
                    progress["clone_name"] = clone_name
                    progress["original_name"] = original_name
                    self.store.save_progress(job.id, progress)
                phase = "clone_wait"

            if phase == "clone_wait":
                result = adapter.wait_task(str(progress.get("task_id") or ""))
                clone_vm_id = _clone_result_id(adapter, result, str(progress.get("task_id") or ""), clone_name)
                if not clone_vm_id:
                    raise PermanentError("Clone finished but the new VM could not be resolved")
                progress["clone_vm_id"] = clone_vm_id
                progress["task_id"] = ""
                progress["phase"] = "drs" if spec.disable_drs else ("rename_source" if spec.destroy_source else "done")
                self.store.save_progress(job.id, progress)
                phase = progress["phase"]

            if phase == "drs":
                task_id = str(progress.get("task_id") or "")
                if not task_id:
                    try:
                        task_id = adapter.disable_vm_drs(None, vm_id=clone_vm_id, vm_name=clone_name) or ""
                    except Exception as exc:
                        detail = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
                        raise PermanentError(
                            f"Clone {clone_name or clone_vm_id} was created, but DRS pin failed: {detail}"
                        ) from exc
                    progress["task_id"] = task_id
                    progress["phase"] = "drs_wait" if task_id else ("rename_source" if spec.destroy_source else "done")
                    progress["drs_disabled"] = True
                    self.store.save_progress(job.id, progress)
                    phase = progress["phase"]
                else:
                    phase = "drs_wait"

            if phase == "drs_wait":
                try:
                    adapter.wait_task(str(progress.get("task_id") or ""))
                except Exception as exc:
                    detail = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
                    raise PermanentError(
                        f"Clone {clone_name or clone_vm_id} was created, but DRS pin failed: {detail}"
                    ) from exc
                progress["task_id"] = ""
                progress["phase"] = "rename_source" if spec.destroy_source else "done"
                self.store.save_progress(job.id, progress)
                phase = progress["phase"]

            if phase == "rename_source":
                if not source_old_name:
                    short = (clone_vm_id or source_id).replace("vm-", "")[-6:] or "tmp"
                    source_old_name = f"{original_name}-old-{short}"
                    progress["source_old_name"] = source_old_name
                task_id = str(progress.get("task_id") or "")
                if not task_id:
                    task_id = adapter.rename_vm(source_id, source_old_name) or ""
                    progress["task_id"] = task_id
                    progress["phase"] = "rename_source_wait" if task_id else "rename_clone"
                    self.store.save_progress(job.id, progress)
                    phase = progress["phase"]
                else:
                    phase = "rename_source_wait"

            if phase == "rename_source_wait":
                adapter.wait_task(str(progress.get("task_id") or ""))
                progress["task_id"] = ""
                progress["phase"] = "rename_clone"
                self.store.save_progress(job.id, progress)
                phase = "rename_clone"

            if phase == "rename_clone":
                task_id = str(progress.get("task_id") or "")
                if not task_id:
                    task_id = adapter.rename_vm(clone_vm_id, original_name) or ""
                    progress["task_id"] = task_id
                    progress["phase"] = "rename_clone_wait" if task_id else "destroy_source"
                    progress["clone_name"] = original_name
                    self.store.save_progress(job.id, progress)
                    phase = progress["phase"]
                else:
                    phase = "rename_clone_wait"

            if phase == "rename_clone_wait":
                adapter.wait_task(str(progress.get("task_id") or ""))
                progress["task_id"] = ""
                progress["phase"] = "destroy_source"
                self.store.save_progress(job.id, progress)
                phase = "destroy_source"

            if phase == "destroy_source":
                results = adapter.apply_actions([source_id], "destroy")
                failed = [row for row in results if not row.ok]
                if failed:
                    messages = "; ".join(f"{row.name or row.vm_id}: {row.message}" for row in failed)
                    raise PermanentError(messages)
                progress["phase"] = "done"
                self.store.save_progress(job.id, progress)

            return {
                "vm_id": source_id,
                "clone_vm_id": clone_vm_id,
                "name": progress.get("clone_name") or clone_name,
                "destroy_source": bool(spec.destroy_source),
                "drs_disabled": bool(progress.get("drs_disabled")),
            }

        if kind == "drs_override":
            spec = DrsOverrideRequest.model_validate(payload)
            vm_ids = [item.strip() for item in spec.vm_ids if item and item.strip()]
            index = int(progress.get("index") or 0)
            results = list(progress.get("results") or [])
            while index < len(vm_ids):
                task_id = str(progress.get("task_id") or "")
                phase = str(progress.get("phase") or "start")
                if phase == "start":
                    task_id = adapter.disable_vm_drs(None, vm_id=vm_ids[index]) or ""
                    progress["task_id"] = task_id
                    progress["index"] = index
                    progress["phase"] = "wait" if task_id else "next"
                    self.store.save_progress(job.id, progress)
                    phase = progress["phase"]
                if phase == "wait":
                    adapter.wait_task(str(progress.get("task_id") or ""))
                    progress["phase"] = "next"
                    self.store.save_progress(job.id, progress)
                results.append({"vm_id": vm_ids[index], "ok": True, "skipped": not bool(progress.get("task_id"))})
                index += 1
                progress = {"index": index, "phase": "start", "task_id": "", "results": results}
                self.store.save_progress(job.id, progress)
            return {"results": results}

        if kind == "rename":
            spec = RenameVmRequest.model_validate(payload)
            vm_id = str(payload.get("vm_id") or "").strip()
            name = spec.name.strip()
            if not vm_id:
                raise PermanentError("vm_id is required")
            task_id = str(progress.get("task_id") or "")
            if not task_id:
                task_id = adapter.rename_vm(vm_id, name) or ""
                progress["task_id"] = task_id
                self.store.save_progress(job.id, progress)
            if task_id:
                adapter.wait_task(task_id)
            return {"vm_id": vm_id, "name": name}

        if kind == "migrate":
            spec = MigrateVmRequest.model_validate(payload)
            vm_ids = list(spec.vm_ids)
            folder_id = (spec.folder_id or "").strip()
            index = int(progress.get("index") or 0)
            results = list(progress.get("results") or [])
            while index < len(vm_ids):
                task_id = str(progress.get("task_id") or "")
                phase = str(progress.get("phase") or "start")
                did_work = bool(progress.get("did_work"))
                if phase == "start":
                    one = spec.model_copy(update={"vm_ids": [vm_ids[index]]})
                    task_id = adapter.start_migrate(one) or ""
                    progress["task_id"] = task_id
                    progress["index"] = index
                    progress["did_work"] = bool(task_id)
                    progress["phase"] = "wait" if task_id else "folder"
                    self.store.save_progress(job.id, progress)
                    phase = progress["phase"]
                    did_work = bool(progress.get("did_work"))
                if phase == "wait":
                    adapter.wait_task(str(progress.get("task_id") or ""))
                    progress["task_id"] = ""
                    progress["did_work"] = True
                    progress["phase"] = "folder"
                    self.store.save_progress(job.id, progress)
                    phase = "folder"
                    did_work = True
                if phase == "folder":
                    move_task = ""
                    if folder_id:
                        move_task = adapter.start_move_into_folder(vm_ids[index], folder_id) or ""
                    progress["task_id"] = move_task
                    if move_task:
                        progress["did_work"] = True
                        did_work = True
                    progress["phase"] = "folder_wait" if move_task else "next"
                    self.store.save_progress(job.id, progress)
                    phase = progress["phase"]
                if phase == "folder_wait":
                    adapter.wait_task(str(progress.get("task_id") or ""))
                    progress["did_work"] = True
                    did_work = True
                    progress["phase"] = "next"
                    self.store.save_progress(job.id, progress)
                results.append(
                    {
                        "vm_id": vm_ids[index],
                        "ok": True,
                        "skipped": not did_work,
                    }
                )
                index += 1
                progress = {"index": index, "phase": "start", "task_id": "", "results": results}
                self.store.save_progress(job.id, progress)
            return {"results": results}

        if kind == "disk_convert":
            plan = DiskConversionPlan.model_validate(payload.get("plan") or {})
            fresh = adapter.disk_conversion_plan(
                DiskConversionPlanRequest(vm_id=plan.vm_id, target=plan.target, method=plan.method)
            )
            if fresh.plan_token != plan.plan_token:
                raise PermanentError("The VM changed after this disk plan was confirmed")
            if not fresh.can_execute:
                raise PermanentError("; ".join(fresh.blockers) or "Disk conversion is no longer safe")
            if plan.method == "ssh":
                def disk_progress(index: int, extra: dict) -> None:
                    self.store.save_progress(job.id, dict(extra))

                return adapter.execute_ssh_disk_conversion(fresh, on_progress=disk_progress)
            task_id = str(progress.get("task_id") or "")
            if not task_id:
                task_id = adapter.start_migrate(
                    MigrateVmRequest(
                        vm_ids=[plan.vm_id],
                        disk_provisioning=plan.target,
                        confirm=True,
                    )
                )
                if not task_id:
                    raise PermanentError("ESXi did not start a disk relocation task")
                progress.update({"task_id": task_id, "phase": "relocate"})
                self.store.save_progress(job.id, progress)
            adapter.wait_task(task_id)
            return {"vm_id": plan.vm_id, "target": plan.target, "method": "soap", "task_id": task_id}

        if kind == "tools_deploy":
            return self._deploy_guest_tools(job, adapter, progress)

        if kind == "host_action":
            spec = HostActionRequest.model_validate(payload)
            task_id = str(progress.get("task_id") or "")
            if not task_id:
                task_id = adapter.start_host_action(spec.action, spec.timeout_seconds)
                progress.update({"task_id": task_id, "phase": "wait"})
                self.store.save_progress(job.id, progress)
            adapter.wait_task(task_id)
            return {"action": spec.action, "task_id": task_id}

        if kind == "host_service":
            spec = HostServiceActionRequest.model_validate(payload)
            return adapter.host_service_action(spec.service_key, spec.action, spec.policy)

        if kind == "host_time":
            spec = HostTimeRequest.model_validate(payload)
            return adapter.configure_host_time(spec.ntp_servers, spec.sync_now)

        if kind == "host_storage_rescan":
            return adapter.rescan_storage()

        if kind == "host_support_bundle":
            task_id = str(progress.get("task_id") or "")
            if not task_id:
                task_id = adapter.start_support_bundle()
                progress.update({"task_id": task_id, "phase": "collect"})
                self.store.save_progress(job.id, progress)
            result = adapter.wait_task(task_id)
            bundles = []
            for item in result or []:
                bundles.append(
                    {
                        "url": str(getattr(item, "url", "") or ""),
                        "error": str(getattr(item, "error", "") or ""),
                    }
                )
            return {"task_id": task_id, "bundles": bundles}

        if kind == "mkdir":
            adapter.mkdir(payload["datastore_id"], payload["path"])
            return payload

        if kind == "delete_file":
            adapter.delete_file(payload["datastore_id"], payload["path"])
            return payload

        if kind == "upload":
            extra = dict(progress)
            extra.update(adapter.upload_file(
                datastore_id=payload["datastore_id"],
                remote_path=payload["remote_path"],
                local_path=payload["local_path"],
                extra=extra,
                on_progress=persist,
                use_library=bool(payload.get("use_library", True)),
            ))
            self.store.save_progress(job.id, extra)
            return extra

        raise PermanentError(f"Unknown job kind {kind}")

    def _deploy_guest_tools(self, job: Job, adapter: InventoryAdapter, progress: dict) -> dict:
        from . import guest_tools

        if self.automation_credentials is None:
            raise PermanentError("The Automation Vault is unavailable")
        payload = job.payload
        credential_id = str(payload.get("credential_id") or "")
        credential = self.automation_credentials.get(credential_id)
        if credential is None:
            raise PermanentError("The selected Automation Vault credential no longer exists")
        bound = str(payload.get("_endpoint_fingerprint") or "")
        if credential.scope == "endpoint" and credential.endpoint_fingerprint != bound:
            raise PermanentError("The selected credential belongs to a different vSphere endpoint")
        secret = self.automation_credentials.secret(credential_id)
        if not secret:
            raise PermanentError("The selected Automation Vault credential has no stored secret")
        family = str(payload.get("os_family") or "")
        address = str(payload.get("address") or "")
        vm_id = str(payload.get("vm_id") or "")
        username = credential.username
        if family == "windows" and credential.kind not in {"windows", "service"}:
            raise PermanentError("A Windows or service credential is required for this guest")
        if family == "linux" and credential.kind not in {"ssh", "service"}:
            raise PermanentError("An SSH or service credential is required for this guest")

        status = adapter.tools_status(vm_id)
        if status.get("running"):
            return {"vm_id": vm_id, "os_family": family, "already_running": True, "tools": status}

        phase = str(progress.get("phase") or "preflight")
        if family == "windows":
            options = {
                "transport": str(payload.get("windows_transport") or "http"),
                "port": int(payload.get("windows_port") or 5985),
                "validate_certificate": bool(payload.get("validate_certificate", True)),
            }
            if phase == "preflight":
                progress["preflight"] = guest_tools.windows_preflight(address, username, secret, **options)
                progress["phase"] = "mount"
                self.store.save_progress(job.id, progress)
                phase = "mount"
            if phase == "mount":
                context = adapter.mount_tools_installer(vm_id)
                progress["original_media"] = context.get("media") or {}
                progress["phase"] = "install"
                self.store.save_progress(job.id, progress)
                phase = "install"
            if phase == "install":
                try:
                    progress["install"] = guest_tools.windows_install(address, username, secret, **options)
                except PermanentError:
                    adapter.restore_tools_media(vm_id, dict(progress.get("original_media") or {}))
                    raise
                progress["phase"] = "verify"
                self.store.save_progress(job.id, progress)
        elif family == "linux":
            options = {
                "port": int(payload.get("linux_port") or 22),
                "host_key_sha256": str(payload.get("ssh_host_key_sha256") or ""),
                "sudo": bool(payload.get("sudo", True)),
            }
            if phase == "preflight":
                progress["preflight"] = guest_tools.linux_preflight(address, username, secret, **options)
                progress["phase"] = "install"
                self.store.save_progress(job.id, progress)
                phase = "install"
            if phase == "install":
                progress["install"] = guest_tools.linux_install(address, username, secret, **options)
                progress["phase"] = "verify"
                self.store.save_progress(job.id, progress)
        else:
            raise PermanentError("Unsupported guest operating system for VMware Tools deployment")

        checks = int(progress.get("verification_checks") or 0)
        limit = 180 if family == "windows" else 36
        while checks < limit and not self._halt.is_set():
            status = adapter.tools_status(vm_id)
            checks += 1
            progress.update({"phase": "verify", "verification_checks": checks, "tools": status})
            self.store.save_progress(job.id, progress)
            if status.get("running"):
                if family == "windows":
                    adapter.restore_tools_media(vm_id, dict(progress.get("original_media") or {}))
                progress["phase"] = "done"
                self.store.save_progress(job.id, progress)
                return {"vm_id": vm_id, "os_family": family, "tools": status, "install": progress.get("install") or {}}
            self._halt.wait(5)
        raise PermanentError("VMware Tools did not report as running before the verification timeout")
