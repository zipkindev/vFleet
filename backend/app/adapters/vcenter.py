from __future__ import annotations

import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from ..config import Settings
from ..errors import PermanentError, TransientError, humanize_vcenter_error, is_not_authenticated, is_transient
from ..grouping import resolve_owner
from ..models import (
    ActionResult,
    CloneVmRequest,
    ClusterSummary,
    ConnectionInfo,
    DatastoreFile,
    DatastoreListing,
    DatastoreSummary,
    HostSummary,
    InventorySnapshot,
    MigrateVmRequest,
    NetworkSummary,
    VirtualMachine,
    VmTemplate,
)
from ..reclaim import annotate_idle, build_owner_reports
from ..power import normalize_power_state
from ..transfer import VCenterRest, library_upload, put_file
from ..vm_storage import summarize_disks, normalize_disk_transform
from .base import InventoryAdapter

ALLOWED_ACTIONS = {"start", "shutdown", "power_off", "reboot", "reset", "suspend", "destroy"}


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _moid(obj: Any) -> str:
    if obj is None:
        return ""
    getter = getattr(obj, "_GetMoId", None)
    if callable(getter):
        return str(getter())
    return str(getattr(obj, "_moId", obj))


def _as_host_ids(value: Any) -> List[str]:
    ids: List[str] = []
    seen = set()
    for item in value or []:
        ref = getattr(item, "key", item)
        moid = _moid(ref)
        if moid and moid not in seen:
            seen.add(moid)
            ids.append(moid)
    return ids


def _prop_map(retrieved: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {"obj": retrieved.obj, "id": _moid(retrieved.obj)}
    missing = {name for name in getattr(retrieved, "missingSet", []) or []}
    for prop in retrieved.propSet or []:
        data[prop.name] = prop.val
    data["_missing"] = missing
    return data


def _pct(used: float, total: float) -> float:
    if not total:
        return 0.0
    return round(max(0.0, min(100.0, (used / total) * 100.0)), 1)


class VCenterAdapter(InventoryAdapter):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._si = None
        self._lock = threading.Lock()
        self._cache: Optional[InventorySnapshot] = None
        self._cache_at = 0.0
        self._error = ""
        self._rest = VCenterRest(settings)
        self._ds_meta: Dict[str, Dict[str, str]] = {}

    def connection(self) -> ConnectionInfo:
        try:
            si = self._session()
            about = si.content.about
            return ConnectionInfo(
                mode="vcenter",
                connected=True,
                host=self.settings.vcenter_host,
                user=self.settings.vcenter_user,
                message=f"{about.fullName} ({about.apiVersion})",
                last_sync=datetime.now(timezone.utc),
            )
        except Exception as exc:
            self._error = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
            return ConnectionInfo(
                mode="vcenter",
                connected=False,
                host=self.settings.vcenter_host,
                user=self.settings.vcenter_user,
                message=self._error or "Not connected",
            )

    def snapshot(self) -> InventorySnapshot:
        now = time.time()
        with self._lock:
            if self._cache and now - self._cache_at < self.settings.cache_ttl_seconds:
                return self._cache
        try:
            snapshot = self._load()
        except Exception as exc:
            if not is_not_authenticated(exc):
                raise
            self._drop_session()
            snapshot = self._load()
        with self._lock:
            self._cache = snapshot
            self._cache_at = time.time()
        return snapshot

    def apply_actions(self, vm_ids: Iterable[str], action: str) -> List[ActionResult]:
        if action not in ALLOWED_ACTIONS:
            return [
                ActionResult(vm_id=vm_id, name="", action=action, ok=False, message="Unsupported action")
                for vm_id in vm_ids
            ]

        from pyVmomi import vim

        content = self._session().content
        results: List[ActionResult] = []
        for vm_id in vm_ids:
            vm = self._find_vm(content, vm_id)
            if vm is None:
                results.append(ActionResult(vm_id=vm_id, name="", action=action, ok=False, message="VM not found"))
                continue
            name = vm.name
            try:
                if action == "start":
                    if str(vm.runtime.powerState) == "poweredOn":
                        results.append(ActionResult(vm_id=vm_id, name=name, action=action, ok=True, message="Already powered on"))
                        continue
                    self.wait_task(_moid(vm.PowerOn()))
                elif action == "power_off":
                    if str(vm.runtime.powerState) == "poweredOff":
                        results.append(ActionResult(vm_id=vm_id, name=name, action=action, ok=True, message="Already powered off"))
                        continue
                    self.wait_task(_moid(vm.PowerOff()))
                elif action == "reset":
                    self.wait_task(_moid(vm.Reset()))
                elif action == "suspend":
                    if str(vm.runtime.powerState) == "suspended":
                        results.append(ActionResult(vm_id=vm_id, name=name, action=action, ok=True, message="Already suspended"))
                        continue
                    self.wait_task(_moid(vm.Suspend()))
                elif action == "shutdown":
                    vm.ShutdownGuest()
                elif action == "reboot":
                    vm.RebootGuest()
                elif action == "destroy":
                    if bool(getattr(getattr(vm, "config", None), "template", False)):
                        results.append(
                            ActionResult(
                                vm_id=vm_id,
                                name=name,
                                action=action,
                                ok=False,
                                message="Refusing to delete a template",
                            )
                        )
                        continue
                    if str(vm.runtime.powerState) != "poweredOff":
                        self.wait_task(_moid(vm.PowerOff()))
                    self.wait_task(_moid(vm.Destroy()))
                results.append(ActionResult(vm_id=vm_id, name=name, action=action, ok=True, message="Accepted"))
            except vim.fault.ToolsUnavailable:
                results.append(
                    ActionResult(
                        vm_id=vm_id,
                        name=name,
                        action=action,
                        ok=False,
                        message="VMware Tools is not available; use power_off or reset",
                    )
                )
            except vim.fault.InvalidPowerState as exc:
                already = "already" in str(exc.msg).lower() or "invalid power state" in str(exc.msg).lower()
                results.append(
                    ActionResult(
                        vm_id=vm_id,
                        name=name,
                        action=action,
                        ok=already,
                        message=str(exc.msg),
                    )
                )
            except Exception as exc:
                if is_not_authenticated(exc):
                    self._drop_session()
                results.append(
                    ActionResult(
                        vm_id=vm_id,
                        name=name,
                        action=action,
                        ok=False,
                        message=humanize_vcenter_error(exc, host=self.settings.vcenter_host),
                    )
                )
        with self._lock:
            self._cache = None
        return results

    def close(self) -> None:
        self._rest.close()
        self._drop_session()

    def _drop_session(self) -> None:
        if self._si is None:
            return
        try:
            from pyVim.connect import Disconnect

            Disconnect(self._si)
        except Exception:
            pass
        self._si = None

    def _session(self):
        if self._si is not None:
            try:
                self._si.CurrentTime()
                return self._si
            except Exception:
                self._drop_session()
        self._si = self._connect()
        return self._si

    def _connect(self):
        from pyVim.connect import SmartConnect

        kwargs = {
            "host": self.settings.vcenter_host,
            "user": self.settings.vcenter_user,
            "pwd": self.settings.vcenter_password,
            "port": self.settings.vcenter_port,
        }
        previous = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.settings.connect_timeout_seconds)
        try:
            try:
                return SmartConnect(disableSslCertValidation=self.settings.vcenter_insecure, **kwargs)
            except TypeError:
                context = None
                if self.settings.vcenter_insecure:
                    context = ssl._create_unverified_context()
                return SmartConnect(sslContext=context, **kwargs)
        finally:
            socket.setdefaulttimeout(previous)

    def _load(self) -> InventorySnapshot:
        from pyVmomi import vim, vmodl

        si = self._session()
        content = si.content
        field_names = {field.key: field.name for field in (content.customFieldsManager.field or [])}
        activity = self._last_activity(content)

        host_rows = self._collect(
            content,
            vmodl,
            vim.HostSystem,
            [
                "name",
                "parent",
                "runtime.connectionState",
                "runtime.powerState",
                "summary.hardware.numCpuCores",
                "summary.hardware.cpuMhz",
                "summary.hardware.memorySize",
                "summary.quickStats.overallCpuUsage",
                "summary.quickStats.overallMemoryUsage",
            ],
        )
        vm_rows = self._collect(
            content,
            vmodl,
            vim.VirtualMachine,
            [
                "name",
                "runtime.powerState",
                "runtime.bootTime",
                "runtime.host",
                "config.hardware.numCPU",
                "config.hardware.memoryMB",
                "config.guestId",
                "config.annotation",
                "guest.toolsStatus",
                "guest.toolsRunningStatus",
                "guest.ipAddress",
                "summary.quickStats.overallCpuUsage",
                "summary.quickStats.guestMemoryUsage",
                "summary.runtime.maxCpuUsage",
                "summary.storage.committed",
                "summary.storage.uncommitted",
                "config.hardware.device",
                "customValue",
                "summary.config.template",
            ],
        )

        hosts: List[HostSummary] = []
        host_by_id: Dict[str, HostSummary] = {}
        cluster_hosts: Dict[str, List[HostSummary]] = {}
        cluster_names: Dict[str, str] = {}

        for row in host_rows:
            parent = row.get("parent")
            cluster_id = _moid(parent) if parent is not None else "standalone"
            cluster_name = getattr(parent, "name", None) or "Standalone"
            cluster_names[cluster_id] = cluster_name
            cores = int(row.get("summary.hardware.numCpuCores") or 0)
            mhz = int(row.get("summary.hardware.cpuMhz") or 0)
            mem_bytes = int(row.get("summary.hardware.memorySize") or 0)
            cpu_usage = int(row.get("summary.quickStats.overallCpuUsage") or 0)
            mem_usage = int(row.get("summary.quickStats.overallMemoryUsage") or 0)
            mem_mib = int(mem_bytes / (1024 * 1024)) if mem_bytes else 0
            host = HostSummary(
                id=row["id"],
                name=str(row.get("name") or row["id"]),
                cluster_id=cluster_id,
                cluster_name=cluster_name,
                connection_state=str(row.get("runtime.connectionState") or "unknown"),
                power_state=normalize_power_state(row.get("runtime.powerState") or "UNKNOWN"),
                cpu_cores=cores,
                cpu_mhz=mhz,
                cpu_usage_mhz=cpu_usage,
                cpu_usage_pct=_pct(cpu_usage, cores * mhz),
                memory_mib=mem_mib,
                memory_usage_mib=mem_usage,
                memory_usage_pct=_pct(mem_usage, mem_mib),
            )
            hosts.append(host)
            host_by_id[host.id] = host
            cluster_hosts.setdefault(cluster_id, []).append(host)

        vms: List[VirtualMachine] = []
        for row in vm_rows:
            if row.get("summary.config.template"):
                continue
            host_ref = row.get("runtime.host")
            host = host_by_id.get(_moid(host_ref)) if host_ref is not None else None
            custom = {}
            for item in row.get("customValue") or []:
                label = field_names.get(item.key, str(item.key))
                custom[label] = str(item.value)
            name = str(row.get("name") or row["id"])
            owner, source = resolve_owner(name, custom, self.settings.owner_fields, self.settings.name_group_pattern)
            cpus = int(row.get("config.hardware.numCPU") or 0)
            memory_mib = int(row.get("config.hardware.memoryMB") or 0)
            cpu_usage = int(row.get("summary.quickStats.overallCpuUsage") or 0)
            mem_usage = int(row.get("summary.quickStats.guestMemoryUsage") or 0)
            max_cpu = int(row.get("summary.runtime.maxCpuUsage") or 0)
            if not max_cpu and host is not None:
                max_cpu = cpus * host.cpu_mhz
            tools = str(row.get("guest.toolsRunningStatus") or row.get("guest.toolsStatus") or "unknown")
            boot = _utc(row.get("runtime.bootTime"))
            last_ts, last_src = activity.get(row["id"], (boot, "boot_time" if boot else "unknown"))
            if boot and (last_ts is None or boot > last_ts):
                last_ts, last_src = boot, "boot_time"
            storage_used, storage_provisioned, disk_provisioning = summarize_disks(
                row.get("config.hardware.device"),
                committed=int(row.get("summary.storage.committed") or 0),
                uncommitted=int(row.get("summary.storage.uncommitted") or 0),
            )
            vm = VirtualMachine(
                id=row["id"],
                name=name,
                power_state=normalize_power_state(row.get("runtime.powerState") or "UNKNOWN"),
                cpu_count=cpus,
                memory_mib=memory_mib,
                cpu_usage_mhz=cpu_usage,
                cpu_usage_pct=_pct(cpu_usage, max_cpu),
                memory_usage_mib=mem_usage,
                memory_usage_pct=_pct(mem_usage, memory_mib),
                host_id=host.id if host else "",
                host_name=host.name if host else "",
                cluster_id=host.cluster_id if host else "",
                cluster_name=host.cluster_name if host else "",
                guest_os=str(row.get("config.guestId") or ""),
                tools_status=tools,
                ip_address=row.get("guest.ipAddress") or None,
                boot_time=boot,
                last_activity=_utc(last_ts),
                last_activity_source=last_src,
                owner_key=owner,
                owner_source=source,
                custom_fields=custom,
                annotation=str(row.get("config.annotation") or ""),
                storage_used_bytes=storage_used,
                storage_provisioned_bytes=storage_provisioned,
                disk_provisioning=disk_provisioning,
            )
            vms.append(annotate_idle(vm, self.settings))

        for host in hosts:
            host.vm_count = sum(1 for vm in vms if vm.host_id == host.id)

        clusters: List[ClusterSummary] = []
        for cluster_id, members in cluster_hosts.items():
            cpu_cap = sum(h.cpu_cores * h.cpu_mhz for h in members)
            mem_cap = sum(h.memory_mib for h in members)
            cpu_use = sum(h.cpu_usage_mhz for h in members)
            mem_use = sum(h.memory_usage_mib for h in members)
            clusters.append(
                ClusterSummary(
                    id=cluster_id,
                    name=cluster_names.get(cluster_id, cluster_id),
                    host_count=len(members),
                    vm_count=sum(1 for vm in vms if vm.cluster_id == cluster_id),
                    cpu_cores=sum(h.cpu_cores for h in members),
                    cpu_usage_mhz=cpu_use,
                    cpu_capacity_mhz=cpu_cap,
                    cpu_usage_pct=_pct(cpu_use, cpu_cap),
                    memory_mib=mem_cap,
                    memory_usage_mib=mem_use,
                    memory_usage_pct=_pct(mem_use, mem_cap),
                )
            )
        clusters.sort(key=lambda item: item.name.lower())
        hosts.sort(key=lambda item: item.name.lower())
        vms.sort(key=lambda item: item.name.lower())

        conn = self.connection()
        conn.last_sync = datetime.now(timezone.utc)
        return InventorySnapshot(
            connection=conn,
            clusters=clusters,
            hosts=hosts,
            vms=vms,
            owners=build_owner_reports(vms),
        )

    def _collect(self, content: Any, vmodl: Any, vimtype: Any, properties: List[str]) -> List[Dict[str, Any]]:
        view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
        try:
            traversal = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseEntities",
                path="view",
                skip=False,
                type=type(view),
            )
            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(obj=view, skip=True, selectSet=[traversal])
            prop_spec = vmodl.query.PropertyCollector.PropertySpec(type=vimtype, pathSet=properties, all=False)
            filter_spec = vmodl.query.PropertyCollector.FilterSpec(objectSet=[obj_spec], propSet=[prop_spec])
            retrieved = content.propertyCollector.RetrieveContents([filter_spec])
            return [_prop_map(item) for item in retrieved]
        finally:
            view.Destroy()

    def _last_activity(self, content: Any) -> Dict[str, Tuple[datetime, str]]:
        latest: Dict[str, Tuple[datetime, str]] = {}
        try:
            from pyVmomi import vim

            begin = datetime.now(timezone.utc) - timedelta(days=self.settings.event_lookback_days)
            spec = vim.event.EventFilterSpec(
                eventTypeId=[
                    "VmAcquiredMksTicketEvent",
                    "VmPoweredOnEvent",
                    "VmPoweredOffEvent",
                    "VmGuestShutdownEvent",
                    "VmGuestRebootEvent",
                    "VmResettingEvent",
                    "VmSuspendedEvent",
                    "VmReconfiguredEvent",
                    "VmMigratedEvent",
                    "VmBeingClonedEvent",
                    "VmCreatedEvent",
                ],
                time=vim.event.EventFilterSpec.ByTime(beginTime=begin.replace(tzinfo=None)),
            )
            collector = content.eventManager.CreateCollectorForEvents(spec)
            try:
                collector.SetCollectorPageSize(200)
                page = list(collector.latestPage or [])
                seen = 0
                while page and seen < 4000:
                    for event in page:
                        vm_info = getattr(event, "vm", None)
                        ref = getattr(vm_info, "vm", None) if vm_info is not None else None
                        if ref is None:
                            continue
                        created = _utc(getattr(event, "createdTime", None))
                        if created is None:
                            continue
                        source = type(event).__name__
                        if source == "VmAcquiredMksTicketEvent":
                            label = "console_event"
                        elif "Power" in source or "Guest" in source or "Reset" in source or "Suspend" in source:
                            label = "power_event"
                        else:
                            label = "vcenter_event"
                        key = _moid(ref)
                        prev = latest.get(key)
                        if prev is None or created > prev[0]:
                            latest[key] = (created, label)
                    seen += len(page)
                    previous = collector.ReadPreviousEvents(200)
                    page = list(previous or [])
            finally:
                collector.DestroyCollector()
        except Exception:
            return latest
        return latest

    def _find_vm(self, content: Any, vm_id: str):
        from pyVmomi import vim

        try:
            vm = self._obj(vim.VirtualMachine, vm_id)
            _ = vm.name
            return vm
        except Exception:
            return None

    def _obj(self, vimtype: Any, moid: str):
        si = self._session()
        return vimtype(moid, si._stub)

    def wait_task(self, task_id: str) -> Any:
        from pyVmomi import vim

        if not task_id:
            return None
        if task_id.startswith("demo-"):
            return None
        last_error = ""
        for _ in range(900):
            try:
                si = self._session()
                task = vim.Task(task_id, si._stub)
                info = task.info
                state = str(info.state)
                if state.endswith("success"):
                    return info.result
                if state.endswith("error"):
                    fault = getattr(info, "error", None)
                    dump = " ".join(
                        part
                        for part in (
                            str(fault) if fault is not None else "",
                            str(getattr(fault, "msg", "") or ""),
                            str(getattr(fault, "privilegeId", "") or ""),
                        )
                        if part
                    ) or "vCenter task failed"
                    raise PermanentError(humanize_vcenter_error(dump, host=self.settings.vcenter_host))
                time.sleep(2)
            except PermanentError:
                raise
            except Exception as exc:
                if is_not_authenticated(exc):
                    self._drop_session()
                last_error = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
                if not is_transient(exc):
                    raise
                self._si = None
                time.sleep(3)
        raise TransientError(last_error or "Timed out waiting for vCenter task")

    def list_datastores(self) -> List[DatastoreSummary]:
        from pyVmomi import vim, vmodl

        rows = self._collect(
            self._session().content,
            vmodl,
            vim.Datastore,
            [
                "name",
                "summary.capacity",
                "summary.freeSpace",
                "summary.type",
                "summary.accessible",
                "host",
                "parent",
            ],
        )
        result: List[DatastoreSummary] = []
        meta: Dict[str, Dict[str, str]] = {}
        for row in rows:
            capacity = int(row.get("summary.capacity") or 0)
            free = int(row.get("summary.freeSpace") or 0)
            used_pct = round(((capacity - free) / capacity) * 100.0, 1) if capacity else 0.0
            parent = row.get("parent")
            datacenter, dc_path = self._datacenter_for(parent)
            hosts = row.get("host") or []
            host_ids = _as_host_ids(hosts)
            item = DatastoreSummary(
                id=row["id"],
                name=str(row.get("name") or row["id"]),
                type=str(row.get("summary.type") or ""),
                capacity_bytes=capacity,
                free_bytes=free,
                accessible=bool(row.get("summary.accessible", True)),
                datacenter=datacenter,
                datacenter_path=dc_path,
                host_count=len(host_ids) or len(list(hosts)),
                host_ids=host_ids,
                usage_pct=used_pct,
            )
            result.append(item)
            meta[item.id] = {"name": item.name, "datacenter": datacenter, "datacenter_path": dc_path}
        result.sort(key=lambda item: item.name.lower())
        self._ds_meta = meta
        return result

    def list_templates(self) -> List[VmTemplate]:
        from pyVmomi import vim, vmodl

        rows = self._collect(
            self._session().content,
            vmodl,
            vim.VirtualMachine,
            [
                "name",
                "config.template",
                "config.hardware.numCPU",
                "config.hardware.memoryMB",
                "config.guestId",
                "runtime.host",
                "datastore",
                "summary.config.template",
            ],
        )
        templates: List[VmTemplate] = []
        for row in rows:
            if not (row.get("config.template") or row.get("summary.config.template")):
                continue
            host_ref = row.get("runtime.host")
            cluster_id = ""
            cluster_name = ""
            if host_ref is not None:
                parent = getattr(host_ref, "parent", None)
                cluster_id = _moid(parent) if parent is not None else ""
                cluster_name = str(getattr(parent, "name", "") or "")
            datastores = list(row.get("datastore") or [])
            ds = datastores[0] if datastores else None
            templates.append(
                VmTemplate(
                    id=row["id"],
                    name=str(row.get("name") or row["id"]),
                    cpu_count=int(row.get("config.hardware.numCPU") or 0),
                    memory_mib=int(row.get("config.hardware.memoryMB") or 0),
                    guest_os=str(row.get("config.guestId") or ""),
                    cluster_id=cluster_id,
                    cluster_name=cluster_name,
                    datastore_id=_moid(ds) if ds is not None else "",
                    datastore_name=str(getattr(ds, "name", "") or ""),
                )
            )
        templates.sort(key=lambda item: item.name.lower())
        return templates

    def list_networks(self) -> List[NetworkSummary]:
        from pyVmomi import vim, vmodl

        rows = self._collect(
            self._session().content,
            vmodl,
            vim.Network,
            ["name", "host", "summary.accessible"],
        )
        result: List[NetworkSummary] = []
        for row in rows:
            obj = row.get("obj")
            type_name = type(obj).__name__ if obj is not None else "Network"
            if "Distributed" in type_name:
                kind = "distributed"
            elif "Opaque" in type_name:
                kind = "opaque"
            else:
                kind = "standard"
            host_ids = _as_host_ids(row.get("host") or [])
            result.append(
                NetworkSummary(
                    id=row["id"],
                    name=str(row.get("name") or row["id"]),
                    type=kind,
                    accessible=bool(row.get("summary.accessible", True)),
                    host_count=len(host_ids),
                    host_ids=host_ids,
                )
            )
        result.sort(key=lambda item: item.name.lower())
        return result

    def browse_datastore(self, datastore_id: str, path: str = "") -> DatastoreListing:
        from pyVmomi import vim

        datastore = self._obj(vim.Datastore, datastore_id)
        spec = vim.host.DatastoreBrowser.SearchSpec(
            details=vim.host.DatastoreBrowser.FileInfo.Details(fileType=True, fileSize=True, modification=True),
            query=[],
        )
        ds_path = f"[{datastore.name}]"
        clean = path.strip("/")
        if clean:
            ds_path = f"[{datastore.name}] {clean}"
        search = getattr(datastore.browser, "SearchDatastore_Task", None) or getattr(
            datastore.browser, "Search", None
        )
        if search is None:
            raise PermanentError("Datastore browser does not support SearchDatastore_Task")
        task_id = _moid(search(datastorePath=ds_path, searchSpec=spec))
        result = self.wait_task(task_id)
        files: List[DatastoreFile] = []
        folder_type = getattr(vim.host.DatastoreBrowser, "FolderInfo", ())
        for info in getattr(result, "file", None) or []:
            name = str(getattr(info, "path", "") or "")
            class_name = type(info).__name__
            is_dir = isinstance(info, folder_type) or "Folder" in class_name
            kind = "folder" if is_dir else "file"
            lowered = name.lower()
            if lowered.endswith(".iso"):
                kind = "iso"
            elif lowered.endswith(".vmdk"):
                kind = "disk"
            files.append(
                DatastoreFile(
                    name=name,
                    path=f"{clean}/{name}".strip("/"),
                    size=int(getattr(info, "fileSize", 0) or 0),
                    is_directory=is_dir,
                    modified=_utc(getattr(info, "modification", None)),
                    kind=kind,
                )
            )
        files.sort(key=lambda item: (not item.is_directory, item.name.lower()))
        return DatastoreListing(datastore_id=datastore_id, datastore_name=datastore.name, path=clean, files=files)

    def start_clone(self, spec: CloneVmRequest) -> str:
        from pyVmomi import vim

        template = self._obj(vim.VirtualMachine, spec.template_id)
        datastore = self._obj(vim.Datastore, spec.datastore_id)
        relocate = vim.vm.RelocateSpec()
        relocate.datastore = datastore
        pool = None
        if spec.cluster_id:
            cluster = self._obj(vim.ClusterComputeResource, spec.cluster_id)
            try:
                pool = cluster.resourcePool
            except Exception:
                compute = self._obj(vim.ComputeResource, spec.cluster_id)
                pool = compute.resourcePool
        if pool is None:
            host = template.runtime.host
            parent = getattr(host, "parent", None) if host is not None else None
            pool = getattr(parent, "resourcePool", None)
        relocate.pool = pool
        clone_spec = vim.vm.CloneSpec(location=relocate, powerOn=bool(spec.power_on), template=False)
        if spec.cpu_count or spec.memory_mib:
            clone_spec.config = vim.vm.ConfigSpec()
            if spec.cpu_count:
                clone_spec.config.numCPUs = int(spec.cpu_count)
            if spec.memory_mib:
                clone_spec.config.memoryMB = int(spec.memory_mib)
        folder = template.parent
        task = template.Clone(folder=folder, name=spec.name.strip(), spec=clone_spec)
        return _moid(task)

    def start_migrate(self, spec: MigrateVmRequest) -> str:
        from pyVmomi import vim

        if not spec.vm_ids:
            raise PermanentError("No virtual machines selected")
        vm = self._find_vm(self._session().content, spec.vm_ids[0])
        if vm is None:
            raise PermanentError("Unknown VM")
        try:
            disk = normalize_disk_transform(spec.disk_provisioning)
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc

        dest_host = self._obj(vim.HostSystem, spec.host_id) if spec.host_id else None
        if dest_host is not None:
            _ = dest_host.name
            current_host = getattr(vm.runtime, "host", None)
            if current_host is not None:
                current_cluster = getattr(current_host, "parent", None)
                dest_cluster = getattr(dest_host, "parent", None)
                if (
                    current_cluster is not None
                    and dest_cluster is not None
                    and _moid(current_cluster) != _moid(dest_cluster)
                ):
                    raise PermanentError(f"{vm.name} is not in the same cluster as {dest_host.name}")

        datastore = self._obj(vim.Datastore, spec.datastore_id) if spec.datastore_id else None
        network = self._obj(vim.Network, spec.network_id) if spec.network_id else None
        if datastore is not None:
            _ = datastore.name
        if network is not None:
            _ = network.name

        relocate = vim.vm.RelocateSpec()
        host_changed = False
        if dest_host is not None:
            current = getattr(vm.runtime, "host", None)
            if current is None or _moid(current) != spec.host_id:
                relocate.host = dest_host
                host_changed = True
            pool = getattr(getattr(dest_host, "parent", None), "resourcePool", None)
            if pool is not None:
                relocate.pool = pool

        if datastore is not None:
            relocate.datastore = datastore

        if disk:
            locators = self._disk_locators(vm, datastore, disk)
            if not locators:
                raise PermanentError(f"{vm.name} has no virtual disks to convert")
            relocate.disk = locators
            relocate.diskMoveType = "moveAllDiskBackingsAndDisallowSharing"
            if relocate.datastore is None:
                first_ds = getattr(locators[0], "datastore", None)
                if first_ds is not None:
                    relocate.datastore = first_ds

        if network is not None:
            changes = self._nic_relocate_specs(vm, network)
            if not changes:
                raise PermanentError(f"{vm.name} has no NICs to reassign")
            relocate.deviceChange = changes

        if not host_changed and datastore is None and not disk and network is None:
            return ""

        task = vm.Relocate(relocate)
        return _moid(task)

    def _disk_locators(self, vm: Any, datastore: Any, provisioning: str) -> List[Any]:
        from pyVmomi import vim

        locators: List[Any] = []
        devices = getattr(getattr(getattr(vm, "config", None), "hardware", None), "device", None) or []
        for device in devices:
            if not isinstance(device, vim.vm.device.VirtualDisk):
                continue
            locator = vim.vm.RelocateSpec.DiskLocator()
            locator.diskId = device.key
            backing = getattr(device, "backing", None)
            current_ds = getattr(backing, "datastore", None) if backing is not None else None
            locator.datastore = datastore if datastore is not None else current_ds
            if locator.datastore is None:
                refs = list(getattr(vm, "datastore", None) or [])
                locator.datastore = refs[0] if refs else None
            if locator.datastore is None:
                raise PermanentError("Cannot convert disk type without a datastore; pick a destination datastore")
            if provisioning:
                info = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
                info.thinProvisioned = provisioning == "thin"
                info.eagerlyScrub = False
                locator.diskBackingInfo = info
            locators.append(locator)
        return locators

    def _nic_relocate_specs(self, vm: Any, network: Any) -> List[Any]:
        from pyVmomi import vim

        backing = self._nic_backing(network)
        changes: List[Any] = []
        devices = getattr(getattr(getattr(vm, "config", None), "hardware", None), "device", None) or []
        for device in devices:
            if not isinstance(device, vim.vm.device.VirtualEthernetCard):
                continue
            spec = vim.vm.device.VirtualDeviceSpec()
            spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            spec.device = device
            spec.device.backing = backing
            changes.append(spec)
        return changes

    def _nic_backing(self, network: Any) -> Any:
        from pyVmomi import vim

        if isinstance(network, vim.dvs.DistributedVirtualPortgroup):
            port = vim.dvs.PortConnection()
            port.portgroupKey = getattr(network, "key", "") or ""
            try:
                dvs = network.config.distributedVirtualSwitch
                port.switchUuid = getattr(dvs, "uuid", "") or ""
            except Exception:
                port.switchUuid = ""
            backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
            backing.port = port
            return backing
        if isinstance(network, vim.OpaqueNetwork):
            summary = getattr(network, "summary", None)
            backing = vim.vm.device.VirtualEthernetCard.OpaqueNetworkBackingInfo()
            backing.opaqueNetworkId = getattr(summary, "opaqueNetworkId", "") or ""
            backing.opaqueNetworkType = getattr(summary, "opaqueNetworkType", "") or "nsx.LogicalSwitch"
            return backing
        backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        backing.network = network
        backing.deviceName = getattr(network, "name", "") or ""
        return backing

    def _session_user(self) -> str:
        try:
            session = self._session().content.sessionManager.currentSession
            return str(getattr(session, "userName", "") or self.settings.vcenter_user)
        except Exception:
            return self.settings.vcenter_user

    def _cluster_or_root(self, cluster_id: str, scope: str):
        from pyVmomi import vim

        if scope == "global":
            return self._session().content.rootFolder, "root folder (global)"
        if cluster_id:
            try:
                cluster = self._obj(vim.ClusterComputeResource, cluster_id)
                name = str(getattr(cluster, "name", "") or cluster_id)
                return cluster, name
            except Exception:
                try:
                    compute = self._obj(vim.ComputeResource, cluster_id)
                    return compute, str(getattr(compute, "name", "") or cluster_id)
                except Exception as exc:
                    raise PermanentError(f"Unknown cluster {cluster_id}") from exc
        return self._session().content.rootFolder, "root folder (global)"

    def _privilege_map(self, entity: Any, user: str) -> Dict[str, bool]:
        from ..migration_access import MIGRATE_PRIV_IDS

        auth = self._session().content.authorizationManager
        granted: Dict[str, bool] = {priv: False for priv in MIGRATE_PRIV_IDS}
        try:
            rows = auth.HasPrivilegeOnEntities([entity], user, MIGRATE_PRIV_IDS)
            flags = list(rows[0]) if rows else []
            for index, priv in enumerate(MIGRATE_PRIV_IDS):
                if index < len(flags):
                    granted[priv] = bool(flags[index])
        except Exception:
            try:
                for priv in MIGRATE_PRIV_IDS:
                    granted[priv] = bool(auth.HasPrivilegeOnEntity(entity, user, [priv])[0])
            except Exception:
                pass
        return granted

    def migration_access(self, cluster_id: str = "") -> Dict[str, Any]:
        from ..migration_access import ROLE_NAME, privilege_rows

        user = self._session_user()
        entity, label = self._cluster_or_root(cluster_id, "cluster" if cluster_id else "global")
        granted = self._privilege_map(entity, user)
        can_modify = False
        try:
            flags = self._session().content.authorizationManager.HasPrivilegeOnEntities(
                [entity],
                user,
                ["Authorization.ModifyPermissions", "Authorization.ModifyRoles"],
            )
            can_modify = any(bool(flag) for flag in (flags[0] if flags else []))
        except Exception:
            can_modify = False
        missing = [priv for priv, ok in granted.items() if not ok and not priv.startswith("System.")]
        if missing:
            message = (
                f"{user} is missing {', '.join(missing)} on {label}. "
                "Enabling assigns the vFleet-Migrate role to this login on that object (propagating to VMs). "
                "Requires Authorization.ModifyPermissions. This does not grant full Administrator."
            )
        else:
            message = f"{user} already has migrate privileges on {label}."
        return {
            "principal": user,
            "scope": "cluster" if cluster_id else "global",
            "cluster_id": cluster_id,
            "cluster_name": label,
            "role_name": ROLE_NAME,
            "can_modify_permissions": can_modify,
            "privileges": privilege_rows(granted),
            "message": message,
        }

    def grant_migration_access(self, cluster_id: str = "", scope: str = "cluster") -> Dict[str, Any]:
        from pyVmomi import vim

        from ..migration_access import MIGRATE_PRIV_IDS, ROLE_NAME

        scope = scope if scope in {"cluster", "global"} else "cluster"
        user = self._session_user()
        entity, label = self._cluster_or_root(cluster_id, scope)
        auth = self._session().content.authorizationManager
        role_id = None
        for role in auth.roleList or []:
            if str(getattr(role, "name", "")) == ROLE_NAME:
                role_id = int(role.roleId)
                try:
                    auth.UpdateAuthorizationRole(roleId=role_id, newName=ROLE_NAME, privIds=MIGRATE_PRIV_IDS)
                except Exception:
                    pass
                break
        if role_id is None:
            try:
                role_id = int(auth.AddAuthorizationRole(name=ROLE_NAME, privIds=MIGRATE_PRIV_IDS))
            except Exception as exc:
                raise PermanentError(
                    humanize_vcenter_error(exc, host=self.settings.vcenter_host)
                    or "Could not create the vFleet-Migrate role. Need Authorization.ModifyRoles."
                ) from exc
        existing = []
        try:
            existing = [
                perm
                for perm in (auth.RetrieveEntityPermissions(entity, False) or [])
                if str(getattr(perm, "principal", "")) == user and not bool(getattr(perm, "group", False))
            ]
        except Exception:
            existing = []
        if existing and int(getattr(existing[0], "roleId", 0)) in {-1}:
            raise PermanentError("This login already has Administrator on that object; not replacing that role.")
        perm = vim.AuthorizationManager.Permission()
        perm.principal = user
        perm.group = False
        perm.roleId = role_id
        perm.propagate = True
        try:
            auth.SetEntityPermissions(entity=entity, permission=[perm])
        except Exception as exc:
            raise PermanentError(
                humanize_vcenter_error(exc, host=self.settings.vcenter_host)
                or "Could not assign migrate permission. Need Authorization.ModifyPermissions on the cluster or root."
            ) from exc
        status = self.migration_access(cluster_id if scope == "cluster" else "")
        status["scope"] = scope
        status["message"] = (
            f"Assigned role {ROLE_NAME} to {user} on {label} (propagate to children). "
            "Queue or retry the migrate job."
        )
        return status

    def mkdir(self, datastore_id: str, path: str) -> None:
        from pyVmomi import vim

        datastore = self._obj(vim.Datastore, datastore_id)
        datacenter = self._find_datacenter(datastore)
        name = f"[{datastore.name}] {path.strip('/')}"
        self._session().content.fileManager.MakeDirectory(name=name, datacenter=datacenter, createParentDirectories=True)

    def delete_file(self, datastore_id: str, path: str) -> None:
        from pyVmomi import vim

        datastore = self._obj(vim.Datastore, datastore_id)
        datacenter = self._find_datacenter(datastore)
        name = f"[{datastore.name}] {path.strip('/')}"
        task = self._session().content.fileManager.DeleteDatastoreFile_Task(name=name, datacenter=datacenter)
        self.wait_task(_moid(task))

    def stat_file(self, datastore_id: str, path: str) -> Optional[int]:
        listing = self.browse_datastore(datastore_id, str(Path(path).parent).replace("\\", "/").strip("."))
        name = Path(path).name
        for item in listing.files:
            if item.name == name and not item.is_directory:
                return item.size
        return None

    def upload_file(
        self,
        datastore_id: str,
        remote_path: str,
        local_path: str,
        extra: Optional[Dict[str, Any]] = None,
        on_progress: Optional[Callable[[int, Dict[str, Any]], None]] = None,
        use_library: bool = True,
    ) -> Dict[str, Any]:
        state = dict(extra or {})
        path = Path(local_path)
        size = path.stat().st_size
        state["bytes_total"] = size
        if use_library:
            try:
                return library_upload(self._rest, datastore_id, Path(remote_path).name or path.name, path, size, state, on_progress)
            except TransientError:
                raise
            except Exception as exc:
                if is_transient(exc):
                    raise TransientError(str(exc)) from exc
                self._rest.reset()
                state["library_fallback"] = str(exc)
                state["phase"] = "datastore_put"

        from pyVmomi import vim

        meta = self._ds_meta.get(datastore_id)
        if meta is None:
            self.list_datastores()
            meta = self._ds_meta.get(datastore_id) or {}
        ds_name = meta.get("name") or self._obj(vim.Datastore, datastore_id).name
        dc_path = meta.get("datacenter_path") or meta.get("datacenter") or ""
        if not dc_path:
            dc_path = self._find_datacenter(self._obj(vim.Datastore, datastore_id)).name
        remote = remote_path.strip("/")
        encoded = "/".join(quote(part, safe="") for part in remote.split("/") if part)
        url = (
            f"https://{self.settings.vcenter_host}:{self.settings.vcenter_port}/folder/{encoded}"
            f"?dcPath={quote(dc_path)}&dsName={quote(ds_name)}"
        )
        cookie = getattr(self._session()._stub, "cookie", "") or ""
        start = int(state.get("bytes_sent") or 0)
        remote_size = None
        try:
            remote_size = self.stat_file(datastore_id, remote)
        except Exception:
            remote_size = None
        if remote_size is not None and remote_size >= size:
            state["bytes_sent"] = size
            if on_progress:
                on_progress(size, state)
            return state
        if remote_size is not None and remote_size > 0:
            start = remote_size
            state["bytes_sent"] = start
        headers = {"Cookie": cookie, "Content-Type": "application/octet-stream"}
        try:
            sent = put_file(
                url,
                path,
                start,
                size,
                headers,
                verify=not self.settings.vcenter_insecure,
                on_progress=on_progress,
                extra=state,
            )
            state["bytes_sent"] = sent
            return state
        except TransientError as exc:
            if start > 0 and "resume" in str(exc).lower():
                state["bytes_sent"] = 0
                sent = put_file(
                    url,
                    path,
                    0,
                    size,
                    headers,
                    verify=not self.settings.vcenter_insecure,
                    on_progress=on_progress,
                    extra=state,
                )
                state["bytes_sent"] = sent
                return state
            raise

    def _datacenter_for(self, parent: Any) -> Tuple[str, str]:
        from pyVmomi import vim

        parts: List[str] = []
        current = parent
        name = ""
        while current is not None:
            if isinstance(current, vim.Datacenter):
                name = current.name
                parts.append(current.name)
                current = getattr(current, "parent", None)
                while current is not None:
                    label = getattr(current, "name", None)
                    if label and label not in {"Datacenters", "ha-folder-root"}:
                        parts.append(str(label))
                    current = getattr(current, "parent", None)
                break
            current = getattr(current, "parent", None)
        parts = [part for part in reversed(parts) if part]
        return name, "/".join(parts) if parts else name

    def _find_datacenter(self, obj: Any):
        from pyVmomi import vim

        current = obj
        while current is not None:
            if isinstance(current, vim.Datacenter):
                return current
            current = getattr(current, "parent", None)
        raise PermanentError("Could not resolve datacenter for datastore")

