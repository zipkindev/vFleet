from __future__ import annotations

import hashlib
import json
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
from ..grouping import resolve_deployed_by, resolve_owner
from ..models import (
    ActionResult,
    Catalog,
    CloneMigrateRequest,
    CloneVmRequest,
    ClusterSummary,
    ConnectionInfo,
    ConsoleTicket,
    DatastoreFile,
    DatastoreListing,
    DatastoreSummary,
    DiskConversionPlan,
    DiskConversionPlanRequest,
    HostHealthSensor,
    HostManagementInfo,
    HostServiceSummary,
    HostStorageAdapterSummary,
    HostSummary,
    InventorySnapshot,
    MigrateVmRequest,
    NetworkSummary,
    VirtualMachine,
    VmFolder,
    VmTemplate,
)
from ..reclaim import annotate_idle, build_owner_reports
from ..power import normalize_power_state
from ..transfer import VCenterRest, library_upload, put_file
from ..vm_storage import disk_summaries, normalize_disk_transform, summarize_disks
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


def datastore_is_readonly(hosts: Any) -> bool:
    modes: List[str] = []
    for mount in hosts or []:
        info = getattr(mount, "mountInfo", None)
        mode = getattr(info, "accessMode", None) if info is not None else None
        if mode:
            modes.append(str(mode).lower())
    return bool(modes) and all(mode == "readonly" for mode in modes)


def first_writable_host_name(datastore: Any) -> str:
    for mount in getattr(datastore, "host", None) or []:
        info = getattr(mount, "mountInfo", None)
        mode = str(getattr(info, "accessMode", "") or "").lower()
        if mode == "readonly":
            continue
        host = getattr(mount, "key", None)
        if host is None:
            continue
        runtime = getattr(host, "runtime", None)
        state = str(getattr(runtime, "connectionState", "") or "")
        if state and state != "connected":
            continue
        if bool(getattr(runtime, "inMaintenanceMode", False)):
            continue
        name = str(getattr(host, "name", "") or "")
        if name:
            return name
    return ""


def folder_file_url(host: str, port: int, remote: str, dc_path: str, ds_name: str) -> str:
    encoded = "/".join(quote(part, safe="") for part in remote.split("/") if part)
    authority = host if port in {0, 443} else f"{host}:{port}"
    return f"https://{authority}/folder/{encoded}?dcPath={quote(dc_path)}&dsName={quote(ds_name)}"


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

    def _endpoint_metadata(self) -> Dict[str, Any]:
        about = self._session().content.about
        api_type = str(getattr(about, "apiType", "") or "")
        endpoint_kind = "esxi" if api_type.lower() == "hostagent" else "vcenter"
        instance_uuid = str(getattr(about, "instanceUuid", "") or "")
        raw = f"{self.settings.vcenter_host.lower()}:{self.settings.vcenter_port}:{api_type}:{instance_uuid}"
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        capabilities = {
            "inventory": True,
            "power": True,
            "console": True,
            "datastores": True,
            "host_admin": endpoint_kind == "esxi",
            "host_services": endpoint_kind == "esxi",
            "host_time": endpoint_kind == "esxi",
            "storage_rescan": endpoint_kind == "esxi",
            "support_bundle": endpoint_kind == "esxi",
            "disk_convert": True,
            "disk_convert_ssh": endpoint_kind == "esxi" and self.settings.esxi_ssh_enabled,
            "clone": endpoint_kind == "vcenter",
            "migrate": endpoint_kind == "vcenter",
            "drs": endpoint_kind == "vcenter",
            "permissions": endpoint_kind == "vcenter",
            "templates": endpoint_kind == "vcenter",
            "content_library": endpoint_kind == "vcenter",
            "historical_metrics": endpoint_kind == "vcenter",
        }
        return {
            "endpoint_kind": endpoint_kind,
            "api_type": api_type,
            "api_version": str(getattr(about, "apiVersion", "") or ""),
            "product_name": str(getattr(about, "fullName", "") or getattr(about, "name", "") or ""),
            "product_version": str(getattr(about, "version", "") or ""),
            "product_build": str(getattr(about, "build", "") or ""),
            "instance_uuid": instance_uuid,
            "endpoint_fingerprint": fingerprint,
            "capabilities": capabilities,
        }

    def endpoint_fingerprint(self) -> str:
        return str(self._endpoint_metadata()["endpoint_fingerprint"])

    def _is_esxi(self) -> bool:
        return self._endpoint_metadata()["endpoint_kind"] == "esxi"

    def connection(self) -> ConnectionInfo:
        try:
            si = self._session()
            about = si.content.about
            metadata = self._endpoint_metadata()
            return ConnectionInfo(
                mode=metadata["endpoint_kind"],
                connected=True,
                host=self.settings.vcenter_host,
                user=self.settings.vcenter_user,
                message=f"{about.fullName} ({about.apiVersion})",
                last_sync=datetime.now(timezone.utc),
                ssh_configured=bool(self.settings.esxi_ssh_enabled),
                **metadata,
            )
        except Exception as exc:
            self._error = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
            return ConnectionInfo(
                mode="esxi" if self.settings.app_mode == "esxi" else "vcenter",
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

    def historical_metrics(self, snapshot: InventorySnapshot, catalog: Optional[Catalog] = None):
        """Pull vCenter historical performance (about 2 weeks) when the stats service has it."""
        from pyVmomi import vim

        from ..metrics import HISTORY_DAYS, MetricSample

        si = self._session()
        perf = si.content.perfManager
        counter_map: Dict[str, tuple[int, str]] = {}
        for counter in perf.perfCounter or []:
            group = getattr(getattr(counter, "groupInfo", None), "key", "")
            name = getattr(getattr(counter, "nameInfo", None), "key", "")
            rollup = str(getattr(counter, "rollupType", "")).split(".")[-1].lower()
            unit = getattr(getattr(counter, "unitInfo", None), "key", "") or ""
            if group and name:
                counter_map[f"{group}.{name}.{rollup}"] = (int(counter.key), unit)

        cpu_meta = counter_map.get("cpu.usage.average")
        mem_meta = counter_map.get("mem.usage.average")
        disk_used_meta = counter_map.get("disk.used.latest") or counter_map.get("disk.capacity.latest")
        disk_cap_meta = counter_map.get("disk.capacity.latest") or counter_map.get("disk.provisioned.latest")
        if not cpu_meta and not mem_meta and not disk_used_meta:
            return []

        end = datetime.now(timezone.utc)
        direct_esxi = self._is_esxi()
        start = end - timedelta(days=1 if direct_esxi else HISTORY_DAYS)
        interval = 300 if direct_esxi else 1800
        naive_start = start.replace(tzinfo=None)
        naive_end = end.replace(tzinfo=None)

        def metric_ids(*metas: Optional[tuple[int, str]]) -> List[Any]:
            ids = []
            for meta in metas:
                if meta:
                    ids.append(vim.PerformanceManager.MetricId(counterId=meta[0], instance=""))
            return ids

        specs: List[Any] = []
        kinds: List[tuple[str, str]] = []
        for host in snapshot.hosts:
            ids = metric_ids(cpu_meta, mem_meta)
            if not ids:
                break
            try:
                specs.append(
                    vim.PerformanceManager.QuerySpec(
                        entity=self._obj(vim.HostSystem, host.id),
                        metricId=ids,
                        startTime=naive_start,
                        endTime=naive_end,
                        intervalId=interval,
                    )
                )
                kinds.append(("host", host.id))
            except Exception:
                continue
        stores = list(catalog.datastores) if catalog is not None else []
        for store in stores:
            ids = metric_ids(disk_used_meta, disk_cap_meta)
            if not ids:
                break
            try:
                specs.append(
                    vim.PerformanceManager.QuerySpec(
                        entity=self._obj(vim.Datastore, store.id),
                        metricId=ids,
                        startTime=naive_start,
                        endTime=naive_end,
                        intervalId=interval,
                    )
                )
                kinds.append(("datastore", store.id))
            except Exception:
                continue
        if not specs:
            return []

        results: List[Any] = []
        for offset in range(0, len(specs), 8):
            chunk = specs[offset : offset + 8]
            try:
                results.extend(perf.QueryPerf(querySpec=chunk) or [])
            except Exception:
                try:
                    for spec in chunk:
                        spec.intervalId = 300
                    results.extend(perf.QueryPerf(querySpec=chunk) or [])
                except Exception:
                    continue

        host_by_id = {host.id: host for host in snapshot.hosts}
        store_by_id = {store.id: store for store in stores}
        entity_kind = {entity_id: kind for kind, entity_id in kinds}

        def as_pct(raw: float, unit: str) -> float:
            value = float(raw)
            if unit == "percent" and value > 100:
                value = value / 100.0
            return round(max(0.0, min(100.0, value)), 2)

        samples: List[MetricSample] = []
        host_points: Dict[datetime, List[MetricSample]] = {}
        for entity_metric in results:
            entity = getattr(entity_metric, "entity", None)
            moid = _moid(entity)
            kind = entity_kind.get(moid)
            if kind is None:
                type_name = type(entity).__name__ if entity is not None else ""
                if "Host" in type_name:
                    kind = "host"
                elif "Datastore" in type_name:
                    kind = "datastore"
                else:
                    continue
            times = [item.timestamp.replace(tzinfo=timezone.utc) if getattr(item.timestamp, "tzinfo", None) is None else item.timestamp.astimezone(timezone.utc) for item in (entity_metric.sampleInfo or [])]
            series: Dict[int, List[int]] = {}
            for value in entity_metric.value or []:
                series[int(value.id.counterId)] = list(value.value or [])
            if kind == "host":
                host = host_by_id.get(moid)
                cpu_id = cpu_meta[0] if cpu_meta else -1
                mem_id = mem_meta[0] if mem_meta else -1
                cpu_unit = cpu_meta[1] if cpu_meta else ""
                mem_unit = mem_meta[1] if mem_meta else ""
                for index, ts in enumerate(times):
                    cpu_vals = series.get(cpu_id) or []
                    mem_vals = series.get(mem_id) or []
                    cpu_pct = as_pct(cpu_vals[index], cpu_unit) if index < len(cpu_vals) else (host.cpu_usage_pct if host else 0.0)
                    mem_pct = as_pct(mem_vals[index], mem_unit) if index < len(mem_vals) else (host.memory_usage_pct if host else 0.0)
                    cpu_mhz = int((cpu_pct / 100.0) * host.cpu_cores * host.cpu_mhz) if host else 0
                    mem_mib = int((mem_pct / 100.0) * host.memory_mib) if host else 0
                    sample = MetricSample(
                        ts=ts,
                        owner_key="",
                        cpu_pct=cpu_pct,
                        memory_pct=mem_pct,
                        disk_pct=0.0,
                        cpu_usage_mhz=cpu_mhz,
                        memory_usage_mib=mem_mib,
                        storage_bytes=0,
                        host_id=moid,
                    )
                    samples.append(sample)
                    host_points.setdefault(ts.replace(second=0, microsecond=0), []).append(sample)
            elif kind == "datastore":
                store = store_by_id.get(moid)
                used_id = disk_used_meta[0] if disk_used_meta else -1
                cap_id = disk_cap_meta[0] if disk_cap_meta else -1
                for index, ts in enumerate(times):
                    used_vals = series.get(used_id) or []
                    cap_vals = series.get(cap_id) or []
                    used = float(used_vals[index]) if index < len(used_vals) else 0.0
                    cap = float(cap_vals[index]) if index < len(cap_vals) else (store.capacity_bytes / 1024 if store else 0.0)
                    if cap <= 0 and store:
                        cap = store.capacity_bytes / 1024.0
                    # vCenter disk counters are typically KB
                    used_bytes = int(used * 1024)
                    cap_bytes = int(cap * 1024) if cap else (store.capacity_bytes if store else 0)
                    pct = (used_bytes / cap_bytes * 100.0) if cap_bytes else (store.usage_pct if store else 0.0)
                    samples.append(
                        MetricSample(
                            ts=ts,
                            owner_key="",
                            cpu_pct=0.0,
                            memory_pct=0.0,
                            disk_pct=round(max(0.0, min(100.0, pct)), 2),
                            cpu_usage_mhz=0,
                            memory_usage_mib=0,
                            storage_bytes=used_bytes,
                            datastore_id=moid,
                        )
                    )

        for ts, group in host_points.items():
            if not group:
                continue
            cpu_pct = sum(item.cpu_pct for item in group) / len(group)
            mem_pct = sum(item.memory_pct for item in group) / len(group)
            samples.append(
                MetricSample(
                    ts=ts,
                    owner_key="",
                    cpu_pct=round(cpu_pct, 2),
                    memory_pct=round(mem_pct, 2),
                    disk_pct=0.0,
                    cpu_usage_mhz=sum(item.cpu_usage_mhz for item in group),
                    memory_usage_mib=sum(item.memory_usage_mib for item in group),
                    storage_bytes=0,
                )
            )
        return samples

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

    def console_ticket(self, vm_id: str, ticket_type: str = "vmrc") -> ConsoleTicket:
        kind = (ticket_type or "vmrc").strip().lower()
        if kind not in {"vmrc", "webmks"}:
            raise PermanentError("Console type must be vmrc or webmks")

        vm = self._find_vm(self._session().content, vm_id)
        if vm is None:
            raise PermanentError("VM not found")
        name = vm.name
        vcenter_url = self._vcenter_console_url(vm_id)

        rest_type = "WEBMKS" if kind == "webmks" else "VMRC"
        try:
            payload = self._rest.console_ticket(vm_id, rest_type)
            ticket = self._normalize_console_ticket(payload, kind)
            if ticket.get("uri") or ticket.get("ticket"):
                return ConsoleTicket(
                    vm_id=vm_id,
                    name=name,
                    type=kind,
                    uri=str(ticket.get("uri") or ""),
                    host=str(ticket.get("host") or ""),
                    port=int(ticket.get("port") or 0),
                    ticket=str(ticket.get("ticket") or ""),
                    ssl_thumbprint=str(ticket.get("ssl_thumbprint") or ""),
                    vcenter_url=vcenter_url,
                    message="Opened via vCenter REST ticket",
                )
        except PermanentError:
            raise
        except Exception:
            pass

        return self._console_ticket_pyvmomi(vm, vm_id, name, kind, vcenter_url)

    def _vcenter_console_url(self, vm_id: str) -> str:
        host = self.settings.vcenter_host
        port = self.settings.vcenter_port
        authority = host if port in {0, 443} else f"{host}:{port}"
        return f"https://{authority}/ui/app/vm;nav=s/urn:vmomi:VirtualMachine:{vm_id}/console"

    def _normalize_console_ticket(self, payload: Any, kind: str) -> Dict[str, Any]:
        if isinstance(payload, str):
            return {"uri": payload, "ticket": payload}
        if not isinstance(payload, dict):
            return {}
        ticket = str(payload.get("ticket") or "")
        uri = str(payload.get("uri") or "")
        if not uri and ticket.startswith(("vmrc://", "http://", "https://", "wss://")):
            uri = ticket
        host = str(payload.get("host") or "")
        port = int(payload.get("port") or 0)
        thumb = str(payload.get("ssl_thumbprint") or payload.get("sslThumbprint") or "")
        if kind == "webmks" and host and ticket and not uri.startswith("wss://"):
            uri = f"wss://{host}:{port or 443}/ticket/{ticket}"
        elif kind == "vmrc" and not uri and ticket:
            uri = ticket
        return {
            "uri": uri,
            "ticket": ticket or uri,
            "host": host,
            "port": port,
            "ssl_thumbprint": thumb,
        }

    def _console_ticket_pyvmomi(
        self,
        vm: Any,
        vm_id: str,
        name: str,
        kind: str,
        vcenter_url: str,
    ) -> ConsoleTicket:
        from pyVmomi import vim

        try:
            raw = vm.AcquireTicket(ticketType=kind)
        except vim.fault.InvalidPowerState:
            raise PermanentError(
                "Web console requires the VM to be powered on. Use VMRC or power on first."
            ) from None
        except vim.fault.NoPermission as exc:
            raise PermanentError(
                f"Missing VirtualMachine.Interact.ConsoleInteract privilege: {getattr(exc, 'msg', exc)}"
            ) from exc
        except Exception as exc:
            raise PermanentError(humanize_vcenter_error(exc, host=self.settings.vcenter_host)) from exc

        ticket = self._normalize_console_ticket(
            {
                "ticket": getattr(raw, "ticket", ""),
                "host": getattr(raw, "host", ""),
                "port": getattr(raw, "port", 0),
                "sslThumbprint": getattr(raw, "sslThumbprint", ""),
            },
            kind,
        )
        if not ticket.get("uri") and not ticket.get("ticket"):
            raise PermanentError("vCenter did not return a console ticket")
        return ConsoleTicket(
            vm_id=vm_id,
            name=name,
            type=kind,
            uri=str(ticket.get("uri") or ""),
            host=str(ticket.get("host") or ""),
            port=int(ticket.get("port") or 0),
            ticket=str(ticket.get("ticket") or ""),
            ssl_thumbprint=str(ticket.get("ssl_thumbprint") or ""),
            vcenter_url=vcenter_url,
            message="Opened via pyVmomi AcquireTicket",
        )

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

    def ping(self) -> None:
        self._session()

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
        custom_fields_manager = getattr(content, "customFieldsManager", None)
        field_names = {
            field.key: field.name
            for field in (getattr(custom_fields_manager, "field", None) or [])
        }
        activity, deployers = self._event_index(content)

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
                "runtime.inMaintenanceMode",
                "summary.quickStats.uptime",
                "summary.hardware.vendor",
                "summary.hardware.model",
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
        cluster_rows = self._collect(
            content,
            vmodl,
            vim.ClusterComputeResource,
            ["configuration.drsVmConfig"],
        )
        drs_overridden: Dict[str, bool] = {}
        for row in cluster_rows:
            for cfg in row.get("configuration.drsVmConfig") or []:
                vm_ref = getattr(cfg, "key", None)
                if vm_ref is None:
                    continue
                enabled = getattr(cfg, "enabled", None)
                if enabled is False:
                    drs_overridden[_moid(vm_ref)] = True

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
                maintenance_mode=bool(row.get("runtime.inMaintenanceMode") or False),
                uptime_seconds=int(row.get("summary.quickStats.uptime") or 0),
                vendor=str(row.get("summary.hardware.vendor") or ""),
                model=str(row.get("summary.hardware.model") or ""),
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
            deployed_by = resolve_deployed_by(custom, self.settings.owner_fields, deployers.get(row["id"], ""))
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
                deployed_by=deployed_by,
                custom_fields=custom,
                annotation=str(row.get("config.annotation") or ""),
                storage_used_bytes=storage_used,
                storage_provisioned_bytes=storage_provisioned,
                disk_provisioning=disk_provisioning,
                drs_override=drs_overridden.get(row["id"], False),
                disks=disk_summaries(row.get("config.hardware.device") or []),
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

    def _collect(
        self,
        content: Any,
        vmodl: Any,
        vimtype: Any,
        properties: List[str],
        root: Any = None,
    ) -> List[Dict[str, Any]]:
        container = root if root is not None else content.rootFolder
        view = content.viewManager.CreateContainerView(container, [vimtype], True)
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

    def _event_index(self, content: Any) -> Tuple[Dict[str, Tuple[datetime, str]], Dict[str, str]]:
        """Scan recent events for last activity and create/clone principals."""
        latest: Dict[str, Tuple[datetime, str]] = {}
        deployers: Dict[str, Tuple[datetime, str]] = {}
        create_types = {
            "VmCreatedEvent",
            "VmClonedEvent",
            "VmDeployedEvent",
            "VmRegisteredEvent",
        }
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
                    "VmClonedEvent",
                    "VmDeployedEvent",
                    "VmRegisteredEvent",
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
                        key = _moid(ref)
                        if source == "VmAcquiredMksTicketEvent":
                            label = "console_event"
                        elif "Power" in source or "Guest" in source or "Reset" in source or "Suspend" in source:
                            label = "power_event"
                        else:
                            label = "vcenter_event"
                        prev = latest.get(key)
                        if prev is None or created > prev[0]:
                            latest[key] = (created, label)

                        if source in create_types:
                            user = str(getattr(event, "userName", "") or "").strip()
                            if user:
                                prior = deployers.get(key)
                                # Prefer the earliest create/clone event as the deployer.
                                if prior is None or created < prior[0]:
                                    deployers[key] = (created, user)
                    seen += len(page)
                    previous = collector.ReadPreviousEvents(200)
                    page = list(previous or [])
            finally:
                collector.DestroyCollector()
        except Exception:
            return latest, {key: user for key, (_ts, user) in deployers.items()}
        return latest, {key: user for key, (_ts, user) in deployers.items()}

    def _last_activity(self, content: Any) -> Dict[str, Tuple[datetime, str]]:
        activity, _deployers = self._event_index(content)
        return activity

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
            readonly = datastore_is_readonly(hosts)
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
                readonly=readonly,
            )
            result.append(item)
            meta[item.id] = {"name": item.name, "datacenter": datacenter, "datacenter_path": dc_path, "readonly": "1" if readonly else "0"}
        result.sort(key=lambda item: item.name.lower())
        self._ds_meta = meta
        return result

    def list_templates(self) -> List[VmTemplate]:
        if self._is_esxi():
            return []
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

    def list_vm_folders(self, datacenter: str = "") -> List[VmFolder]:
        """List VM inventory folders via PropertyCollector (avoids per-folder childEntity walks)."""
        from pyVmomi import vim, vmodl

        wanted = (datacenter or "").strip().lower()
        result: List[VmFolder] = []
        content = self._session().content
        for dc in content.rootFolder.childEntity:
            if not isinstance(dc, vim.Datacenter):
                continue
            dc_id = _moid(dc)
            dc_name = str(getattr(dc, "name", "") or dc_id)
            if wanted and wanted not in {dc_id.lower(), dc_name.lower()}:
                continue
            root = getattr(dc, "vmFolder", None)
            if root is None:
                continue
            root_id = _moid(root)
            root_name = str(getattr(root, "name", "") or "vm")
            by_id: Dict[str, Dict[str, str]] = {
                root_id: {"name": root_name, "parent_id": ""},
            }
            for row in self._collect(content, vmodl, vim.Folder, ["name", "parent"], root=root):
                folder_id = str(row.get("id") or "")
                if not folder_id or folder_id == root_id:
                    continue
                parent = row.get("parent")
                parent_id = _moid(parent) if parent is not None else ""
                by_id[folder_id] = {
                    "name": str(row.get("name") or folder_id),
                    "parent_id": parent_id,
                }

            path_cache: Dict[str, str] = {}

            def folder_path(folder_id: str) -> str:
                cached = path_cache.get(folder_id)
                if cached is not None:
                    return cached
                node = by_id.get(folder_id)
                if node is None:
                    path_cache[folder_id] = folder_id
                    return folder_id
                parent_id = node["parent_id"]
                if not parent_id or parent_id not in by_id:
                    path = f"{dc_name} / {node['name']}"
                else:
                    path = f"{folder_path(parent_id)} / {node['name']}"
                path_cache[folder_id] = path
                return path

            for folder_id, node in by_id.items():
                result.append(
                    VmFolder(
                        id=folder_id,
                        name=node["name"],
                        path=folder_path(folder_id),
                        parent_id=node["parent_id"],
                        datacenter_id=dc_id,
                        datacenter_name=dc_name,
                    )
                )
        result.sort(key=lambda item: item.path.lower())
        return result

    def _resolve_vm_folder(self, folder_id: str) -> Any:
        from pyVmomi import vim

        clean = (folder_id or "").strip()
        if not clean:
            return None
        folder = self._obj(vim.Folder, clean)
        try:
            _ = folder.name
        except Exception as exc:
            raise PermanentError(f"Unknown VM folder: {clean}") from exc
        parent = folder
        while parent is not None:
            if isinstance(parent, vim.Datacenter):
                return folder
            parent = getattr(parent, "parent", None)
        raise PermanentError(f"Folder {clean} is not under a datacenter VM inventory")

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
        dest_host = self._obj(vim.HostSystem, spec.host_id) if spec.host_id else None
        pool = None
        if dest_host is not None:
            _ = dest_host.name
            connection = str(getattr(dest_host.runtime, "connectionState", "") or "").lower()
            if connection == "disconnected":
                raise PermanentError(f"Host {dest_host.name} is disconnected")
            dest_parent = getattr(dest_host, "parent", None)
            if spec.cluster_id:
                cluster_moid = spec.cluster_id
                if _moid(dest_parent) != cluster_moid:
                    try:
                        compute = self._obj(vim.ComputeResource, spec.cluster_id)
                        if _moid(dest_parent) != _moid(compute):
                            raise PermanentError(f"Host {dest_host.name} is not in the selected cluster")
                    except PermanentError:
                        raise
                    except Exception:
                        raise PermanentError(f"Host {dest_host.name} is not in the selected cluster") from None
            relocate.host = dest_host
            pool = getattr(dest_parent, "resourcePool", None)
        elif spec.cluster_id:
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
        folder = self._resolve_vm_folder(spec.folder_id) or template.parent
        task = template.Clone(folder=folder, name=spec.name.strip(), spec=clone_spec)
        return _moid(task)

    def start_clone_migrate(self, spec: CloneMigrateRequest) -> str:
        from pyVmomi import vim

        source = self._find_vm(self._session().content, spec.vm_id)
        if source is None:
            raise PermanentError("Unknown VM")
        if bool(getattr(getattr(source, "config", None), "template", False)):
            raise PermanentError("Refusing to clone-migrate a template")
        clone_name = (spec.name or "").strip()
        if not clone_name or "/" in clone_name:
            raise PermanentError("Clone name is required and cannot contain '/'")

        dest_host = self._obj(vim.HostSystem, spec.host_id)
        _ = dest_host.name
        connection = str(getattr(dest_host.runtime, "connectionState", "") or "").lower()
        if connection == "disconnected":
            raise PermanentError(f"Host {dest_host.name} is disconnected")
        current_host = getattr(source.runtime, "host", None)
        if current_host is not None:
            current_cluster = getattr(current_host, "parent", None)
            dest_cluster = getattr(dest_host, "parent", None)
            if (
                current_cluster is not None
                and dest_cluster is not None
                and _moid(current_cluster) != _moid(dest_cluster)
            ):
                raise PermanentError(f"{source.name} is not in the same cluster as {dest_host.name}")

        relocate = vim.vm.RelocateSpec()
        relocate.host = dest_host
        pool = getattr(getattr(dest_host, "parent", None), "resourcePool", None)
        if pool is not None:
            relocate.pool = pool

        if spec.datastore_id:
            datastore = self._obj(vim.Datastore, spec.datastore_id)
            relocate.datastore = datastore
        else:
            refs = list(getattr(source, "datastore", None) or [])
            if refs:
                relocate.datastore = refs[0]

        clone_spec = vim.vm.CloneSpec(location=relocate, powerOn=bool(spec.power_on), template=False)
        folder = self._resolve_vm_folder(spec.folder_id) or source.parent
        task = source.Clone(folder=folder, name=clone_name, spec=clone_spec)
        return _moid(task)

    def rename_vm(self, vm_id: str, new_name: str) -> str:
        from pyVmomi import vim

        clean = new_name.strip()
        if not clean or "/" in clean:
            raise PermanentError("Invalid VM name")
        vm = self._find_vm(self._session().content, vm_id)
        if vm is None:
            raise PermanentError("Unknown VM")
        task = vm.Rename(clean)
        return _moid(task)

    def disable_vm_drs(self, vm: Any, cluster_id: str = "", vm_name: str = "", vm_id: str = "") -> str:
        from pyVmomi import vim

        target = vm
        if target is None and (vm_id or "").strip():
            target = self._find_vm(self._session().content, vm_id.strip())
        if target is None and vm_name:
            from pyVmomi import vmodl

            content = self._session().content
            for row in self._collect(content, vmodl, vim.VirtualMachine, ["name"]):
                if str(row.get("name") or "") == vm_name:
                    target = self._find_vm(content, row["id"])
                    break
        if target is None:
            raise PermanentError("Cannot disable DRS: VM was not found")

        cluster = None
        if cluster_id:
            try:
                cluster = self._obj(vim.ClusterComputeResource, cluster_id)
            except Exception:
                cluster = self._obj(vim.ComputeResource, cluster_id)
        if cluster is None:
            host = getattr(getattr(target, "runtime", None), "host", None)
            parent = getattr(host, "parent", None) if host is not None else None
            if isinstance(parent, vim.ClusterComputeResource):
                cluster = parent
            elif parent is not None and hasattr(parent, "parent"):
                grand = getattr(parent, "parent", None)
                if isinstance(grand, vim.ClusterComputeResource):
                    cluster = grand
        if cluster is None:
            raise PermanentError("Cannot disable DRS: VM is not in a DRS cluster")

        drs_vm_config_info = vim.cluster.DrsVmConfigInfo()
        drs_vm_config_info.key = target
        drs_vm_config_info.enabled = False

        drs_config_spec = vim.cluster.DrsVmConfigSpec()
        drs_config_spec.operation = vim.option.ArrayUpdateSpec.Operation.add
        drs_config_spec.info = drs_vm_config_info

        cluster_spec_ex = vim.cluster.ConfigSpecEx()
        cluster_spec_ex.drsVmConfigSpec = [drs_config_spec]

        task = cluster.ReconfigureComputeResource_Task(cluster_spec_ex, True)
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

    def start_move_into_folder(self, vm_id: str, folder_id: str) -> str:
        from pyVmomi import vim

        clean_folder = (folder_id or "").strip()
        if not clean_folder:
            return ""
        vm = self._find_vm(self._session().content, vm_id)
        if vm is None:
            raise PermanentError("Unknown VM")
        folder = self._resolve_vm_folder(clean_folder)
        if folder is None:
            raise PermanentError("Unknown VM folder")
        current = getattr(vm, "parent", None)
        if current is not None and _moid(current) == _moid(folder):
            return ""
        task = folder.MoveIntoFolder_Task([vm])
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
                info.eagerlyScrub = provisioning == "eager_zeroed_thick"
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

    def _host_obj(self):
        from pyVmomi import vim, vmodl

        rows = self._collect(self._session().content, vmodl, vim.HostSystem, ["name"])
        if not rows:
            raise PermanentError("This endpoint did not return an ESXi host")
        if len(rows) > 1 and self._is_esxi():
            raise PermanentError("Direct ESXi mode expected exactly one host")
        return self._obj(vim.HostSystem, rows[0]["id"])

    def host_management(self) -> HostManagementInfo:
        if not self._is_esxi():
            raise PermanentError("Host administration is available when connected directly to an ESXi host")
        host = self._host_obj()
        content = self._session().content
        connection = self.connection()
        runtime = getattr(host, "runtime", None)
        summary = getattr(host, "summary", None)
        hardware = getattr(summary, "hardware", None)
        quick = getattr(summary, "quickStats", None)
        config = getattr(host, "config", None)
        managers = getattr(host, "configManager", None)

        services: List[HostServiceSummary] = []
        service_system = getattr(managers, "serviceSystem", None)
        service_info = getattr(service_system, "serviceInfo", None)
        for item in getattr(service_info, "service", None) or []:
            key = str(getattr(item, "key", "") or "")
            services.append(
                HostServiceSummary(
                    key=key,
                    label=str(getattr(item, "label", "") or key),
                    running=bool(getattr(item, "running", False)),
                    policy=str(getattr(item, "policy", "") or ""),
                    required=bool(getattr(item, "required", False)),
                    controllable=key in {"TSM", "TSM-SSH", "ntpd"},
                )
            )

        storage_adapters: List[HostStorageAdapterSummary] = []
        storage = getattr(config, "storageDevice", None)
        for item in getattr(storage, "hostBusAdapter", None) or []:
            storage_adapters.append(
                HostStorageAdapterSummary(
                    key=str(getattr(item, "key", "") or getattr(item, "device", "") or ""),
                    model=str(getattr(item, "model", "") or ""),
                    driver=str(getattr(item, "driver", "") or ""),
                    status=str(getattr(item, "status", "") or ""),
                    device=str(getattr(item, "device", "") or ""),
                )
            )

        health: List[HostHealthSensor] = []
        health_system = getattr(runtime, "healthSystemRuntime", None)
        system_health = getattr(health_system, "systemHealthInfo", None)
        for item in getattr(system_health, "numericSensorInfo", None) or []:
            health.append(
                HostHealthSensor(
                    name=str(getattr(item, "name", "") or "Sensor"),
                    status=str(getattr(item, "healthState", "") or "unknown"),
                    reading=str(getattr(item, "currentReading", "") or ""),
                )
            )

        date_time = getattr(config, "dateTimeInfo", None)
        ntp_config = getattr(date_time, "ntpConfig", None)
        network = getattr(config, "network", None)
        dns = getattr(network, "dnsConfig", None)
        uptime = int(getattr(quick, "uptime", 0) or 0)
        now = datetime.now(timezone.utc)
        try:
            remote_now = getattr(managers, "dateTimeSystem", None).QueryDateTime()
            remote_now = _utc(remote_now)
        except Exception:
            remote_now = None
        return HostManagementInfo(
            host_id=_moid(host),
            name=str(getattr(host, "name", "") or self.settings.vcenter_host),
            endpoint_kind="esxi",
            product_name=connection.product_name,
            version=connection.product_version,
            build=connection.product_build,
            api_version=connection.api_version,
            vendor=str(getattr(hardware, "vendor", "") or ""),
            model=str(getattr(hardware, "model", "") or ""),
            uuid=str(getattr(hardware, "uuid", "") or ""),
            connection_state=str(getattr(runtime, "connectionState", "") or "unknown"),
            maintenance_mode=bool(getattr(runtime, "inMaintenanceMode", False)),
            uptime_seconds=uptime,
            boot_time=now - timedelta(seconds=uptime) if uptime else None,
            current_time=remote_now,
            ntp_servers=[str(item) for item in getattr(ntp_config, "server", None) or []],
            dns_servers=[str(item) for item in getattr(dns, "address", None) or []],
            search_domains=[str(item) for item in getattr(dns, "searchDomain", None) or []],
            hostname=str(getattr(dns, "hostName", "") or ""),
            domain_name=str(getattr(dns, "domainName", "") or ""),
            services=sorted(services, key=lambda item: item.label.lower()),
            storage_adapters=storage_adapters,
            health=health,
            ssh_configured=bool(self.settings.esxi_ssh_enabled),
            capabilities=connection.capabilities,
        )

    def _require_quiescent_host(self, host: Any, action: str) -> None:
        powered = [
            str(getattr(vm, "name", "") or _moid(vm))
            for vm in getattr(host, "vm", None) or []
            if normalize_power_state(getattr(getattr(vm, "runtime", None), "powerState", "")) == "POWERED_ON"
        ]
        if powered:
            preview = ", ".join(powered[:5])
            suffix = "…" if len(powered) > 5 else ""
            raise PermanentError(f"Cannot {action} while powered-on VMs remain: {preview}{suffix}")

    def start_host_action(self, action: str, timeout_seconds: int = 900) -> str:
        if not self._is_esxi():
            raise PermanentError("Host actions require a direct ESXi connection")
        host = self._host_obj()
        action = (action or "").strip().lower()
        if action == "maintenance_enter":
            self._require_quiescent_host(host, "enter maintenance mode")
            task = host.EnterMaintenanceMode_Task(timeout=int(timeout_seconds), evacuatePoweredOffVms=False)
        elif action == "maintenance_exit":
            task = host.ExitMaintenanceMode_Task(timeout=int(timeout_seconds))
        elif action in {"reboot", "shutdown"}:
            self._require_quiescent_host(host, action)
            if not bool(getattr(getattr(host, "runtime", None), "inMaintenanceMode", False)):
                raise PermanentError(f"Enter maintenance mode before host {action}")
            task = host.RebootHost_Task(force=False) if action == "reboot" else host.ShutdownHost_Task(force=False)
        else:
            raise PermanentError("Host action must be maintenance_enter, maintenance_exit, reboot, or shutdown")
        return _moid(task)

    def host_service_action(self, service_key: str, action: str, policy: str = "") -> Dict[str, Any]:
        if not self._is_esxi():
            raise PermanentError("Host service controls require a direct ESXi connection")
        key = (service_key or "").strip()
        if key not in {"TSM", "TSM-SSH", "ntpd"}:
            raise PermanentError("Only ESXi Shell, SSH, and NTP services are controllable from vFleet")
        service_system = self._host_obj().configManager.serviceSystem
        action = (action or "").strip().lower()
        if action == "start":
            service_system.StartService(id=key)
        elif action == "stop":
            service_system.StopService(id=key)
        elif action == "restart":
            service_system.RestartService(id=key)
        elif action == "policy":
            clean_policy = (policy or "").strip()
            if clean_policy not in {"on", "off", "automatic"}:
                raise PermanentError("Service policy must be on, off, or automatic")
            service_system.UpdateServicePolicy(id=key, policy=clean_policy)
        else:
            raise PermanentError("Service action must be start, stop, restart, or policy")
        return {"service_key": key, "action": action, "policy": policy}

    def configure_host_time(self, ntp_servers: List[str], sync_now: bool = False) -> Dict[str, Any]:
        if not self._is_esxi():
            raise PermanentError("Host time controls require a direct ESXi connection")
        clean = [item.strip() for item in ntp_servers if item and item.strip()]
        if len(clean) > 8 or any(any(ch.isspace() for ch in item) for item in clean):
            raise PermanentError("Provide up to eight valid NTP hostnames or addresses")
        from pyVmomi import vim

        system = self._host_obj().configManager.dateTimeSystem
        ntp = vim.host.NtpConfig(server=clean)
        system.UpdateDateTimeConfig(config=vim.host.DateTimeConfig(ntpConfig=ntp))
        if sync_now:
            try:
                self._host_obj().configManager.serviceSystem.RestartService(id="ntpd")
            except Exception as exc:
                raise PermanentError(f"NTP configuration was saved, but ntpd could not be restarted: {exc}") from exc
        return {"ntp_servers": clean, "sync_now": sync_now}

    def rescan_storage(self) -> Dict[str, Any]:
        if not self._is_esxi():
            raise PermanentError("Storage rescan requires a direct ESXi connection")
        storage = self._host_obj().configManager.storageSystem
        storage.RescanAllHba()
        storage.RescanVmfs()
        return {"rescanned": True}

    def start_support_bundle(self) -> str:
        if not self._is_esxi():
            raise PermanentError("Support bundles require a direct ESXi connection")
        host = self._host_obj()
        diagnostic = host.configManager.diagnosticSystem
        task = diagnostic.GenerateLogBundles_Task(includeDefault=True, host=[host])
        return _moid(task)

    def disk_conversion_plan(self, spec: DiskConversionPlanRequest) -> DiskConversionPlan:
        target = normalize_disk_transform(spec.target)
        if not target:
            raise PermanentError("Choose thin, lazy-zeroed thick, or eager-zeroed thick")
        method = (spec.method or "auto").strip().lower()
        if method not in {"auto", "soap", "ssh"}:
            raise PermanentError("Conversion method must be auto, soap, or ssh")
        vm = self._find_vm(self._session().content, spec.vm_id)
        if vm is None:
            raise PermanentError("Unknown VM")
        devices = getattr(getattr(getattr(vm, "config", None), "hardware", None), "device", None) or []
        disks = disk_summaries(devices)
        power = normalize_power_state(getattr(getattr(vm, "runtime", None), "powerState", ""))
        blockers: List[str] = []
        warnings: List[str] = []
        if not disks:
            blockers.append("The VM has no supported virtual disks")
        for disk in disks:
            if disk.rdm:
                blockers.append(f"{disk.label} is an RDM or device mapping")
            if disk.encrypted:
                blockers.append(f"{disk.label} is encrypted")
            if disk.sharing and disk.sharing.lower() not in {"", "sharingnone"}:
                blockers.append(f"{disk.label} uses shared-disk mode {disk.sharing}")
        noop = bool(disks) and all(disk.provisioning == target for disk in disks)
        direct_esxi = self._is_esxi()
        # A standalone HostAgent can expose relocation capability flags yet reject an
        # in-place provisioning change with vmodl.fault.NotSupported. vCenter has a
        # provisioning checker for this workflow; HostAgent does not. Automatic mode
        # therefore uses the verified, allowlisted SSH implementation on direct ESXi.
        selected = ("ssh" if direct_esxi else "soap") if method == "auto" else method
        if selected == "ssh":
            if not direct_esxi:
                blockers.append("SSH conversion is only available for a direct ESXi connection")
            if not self.settings.esxi_ssh_enabled:
                blockers.append("Verified SSH disk conversion is not configured for this host; edit the connection to enable it")
            if power != "POWERED_OFF":
                blockers.append("SSH conversion requires the VM to be powered off")
            if getattr(vm, "snapshot", None) is not None:
                blockers.append("SSH conversion requires all VM snapshots to be removed first")
            if direct_esxi and self.settings.esxi_ssh_enabled:
                warnings.append(
                    "Verified SSH conversion temporarily starts the ESXi SSH service for the job and restores it afterward"
                )
        else:
            if direct_esxi:
                warnings.append(
                    "Advanced direct-ESXi API relocation may be rejected by HostAgent with NotSupported; verified SSH is recommended"
                )
            else:
                warnings.append(
                    "The vSphere API performs a storage relocation; required API privileges and free space are checked by vCenter"
                )
        change_version = str(getattr(getattr(vm, "config", None), "changeVersion", "") or "")
        token_data = {
            "endpoint": self.endpoint_fingerprint(),
            "vm": spec.vm_id,
            "change": change_version,
            "power": power,
            "target": target,
            "method": selected,
            "disks": [disk.model_dump() for disk in disks],
        }
        token = hashlib.sha256(json.dumps(token_data, sort_keys=True).encode("utf-8")).hexdigest()
        return DiskConversionPlan(
            vm_id=spec.vm_id,
            vm_name=str(getattr(vm, "name", "") or spec.vm_id),
            target=target,
            method=selected,
            fallback_method="ssh" if selected == "soap" and direct_esxi and self.settings.esxi_ssh_enabled else "",
            plan_token=token,
            power_state=power,
            disks=disks,
            blockers=blockers,
            warnings=warnings,
            estimated_scratch_bytes=sum(disk.capacity_bytes for disk in disks),
            noop=noop,
            can_execute=not blockers and not noop,
        )

    def execute_ssh_disk_conversion(
        self,
        plan: DiskConversionPlan,
        on_progress: Optional[Callable[[int, Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        if plan.method != "ssh" or not self._is_esxi() or not self.settings.esxi_ssh_enabled:
            raise PermanentError("This disk plan is not authorized for the ESXi SSH fallback")
        current = self.disk_conversion_plan(
            DiskConversionPlanRequest(vm_id=plan.vm_id, target=plan.target, method="ssh")
        )
        if current.plan_token != plan.plan_token:
            raise PermanentError("The VM changed after this plan was created; review and confirm a new plan")
        if not current.can_execute:
            raise PermanentError("; ".join(current.blockers) or "Disk conversion cannot be executed")

        from pyVmomi import vim

        from ..esxi_ssh import EsxiSshExecutor, datastore_path

        vm = self._find_vm(self._session().content, plan.vm_id)
        if vm is None:
            raise PermanentError("Unknown VM")
        service_system = self._host_obj().configManager.serviceSystem
        services = getattr(getattr(service_system, "serviceInfo", None), "service", None) or []
        ssh_service = next((item for item in services if str(getattr(item, "key", "")) == "TSM-SSH"), None)
        if ssh_service is None:
            raise PermanentError("This ESXi host does not expose the SSH service for temporary activation")
        ssh_started_for_job = not bool(getattr(ssh_service, "running", False))
        if ssh_started_for_job:
            if on_progress:
                on_progress(0, {"phase": "ssh_start", "index": 0, "total": len(plan.disks)})
            service_system.StartService(id="TSM-SSH")
        devices = getattr(getattr(getattr(vm, "config", None), "hardware", None), "device", None) or []
        by_key = {int(getattr(device, "key", 0) or 0): device for device in devices}
        executor = EsxiSshExecutor(self.settings)
        changes: List[Any] = []
        converted: List[Dict[str, str]] = []
        total = len(plan.disks)
        ssh_service_restored = not ssh_started_for_job
        try:
            for index, disk in enumerate(plan.disks):
                _datastore, relative, source = datastore_path(disk.file_name)
                if not relative.lower().endswith(".vmdk"):
                    raise PermanentError(f"{disk.label} does not reference a VMDK descriptor")
                suffix = plan.plan_token[:12]
                relative_dest = f"{relative[:-5]}.vfleet-{suffix}.vmdk"
                _ds2, _rel2, destination = datastore_path(f"[{_datastore}] {relative_dest}")
                if on_progress:
                    on_progress(index, {"phase": "clone", "disk": disk.label, "index": index, "total": total})
                executor.clone_disk(source, destination, plan.target)
                device = by_key.get(disk.key)
                if device is None:
                    raise PermanentError(f"Disk device {disk.key} changed while converting")
                device.backing.fileName = f"[{_datastore}] {relative_dest}"
                change = vim.vm.device.VirtualDeviceSpec()
                change.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
                change.device = device
                changes.append(change)
                converted.append({"label": disk.label, "source": disk.file_name, "destination": device.backing.fileName})

            if on_progress:
                on_progress(total, {"phase": "reconfigure", "index": total, "total": total})
            spec = vim.vm.ConfigSpec(deviceChange=changes)
            task = vm.ReconfigVM_Task(spec=spec)
            self.wait_task(_moid(task))
        finally:
            if ssh_started_for_job:
                if on_progress:
                    on_progress(total, {"phase": "ssh_stop", "index": total, "total": total})
                try:
                    service_system.StopService(id="TSM-SSH")
                    ssh_service_restored = True
                except Exception:
                    ssh_service_restored = False
        self._cache_at = 0.0
        return {
            "vm_id": plan.vm_id,
            "target": plan.target,
            "method": "ssh",
            "converted": converted,
            "source_disks_preserved": True,
            "ssh_service_temporarily_started": ssh_started_for_job,
            "ssh_service_restored": ssh_service_restored,
        }

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
        if self._is_esxi():
            raise PermanentError("vCenter roles and DRS permissions do not exist on a standalone ESXi host")
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
        if self._is_esxi():
            raise PermanentError("vCenter roles and DRS permissions do not exist on a standalone ESXi host")
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
        from pyVmomi import vim

        datastore = self._obj(vim.Datastore, datastore_id)
        if datastore_is_readonly(getattr(datastore, "host", None)):
            raise PermanentError(
                f"Datastore {datastore.name} is mounted read-only on every host. "
                "vCenter cannot write files here. Use a writable datastore, or follow "
                "'How to Upload your own Images.txt' on this datastore."
            )
        if use_library and not self._is_esxi():
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

        meta = self._ds_meta.get(datastore_id)
        if meta is None:
            self.list_datastores()
            meta = self._ds_meta.get(datastore_id) or {}
        ds_name = meta.get("name") or datastore.name
        dc_path = meta.get("datacenter_path") or meta.get("datacenter") or ""
        if not dc_path:
            dc_path = self._find_datacenter(datastore).name
        remote = remote_path.strip("/")
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
        try:
            sent = self._put_datastore_file(datastore, ds_name, dc_path, remote, path, start, size, state, on_progress)
            state["bytes_sent"] = sent
            return state
        except TransientError as exc:
            if start > 0 and "resume" in str(exc).lower():
                state["bytes_sent"] = 0
                sent = self._put_datastore_file(datastore, ds_name, dc_path, remote, path, 0, size, state, on_progress)
                state["bytes_sent"] = sent
                return state
            raise

    def _http_put_ticket(self, url: str) -> str:
        from pyVmomi import vim

        spec = vim.SessionManager.HttpServiceRequestSpec(url=url, method="httpPut")
        ticket = self._session().content.sessionManager.AcquireGenericServiceTicket(spec)
        return str(getattr(ticket, "id", "") or "")

    def _put_datastore_file(
        self,
        datastore: Any,
        ds_name: str,
        dc_path: str,
        remote: str,
        path: Path,
        start: int,
        size: int,
        state: Dict[str, Any],
        on_progress: Optional[Callable[[int, Dict[str, Any]], None]],
    ) -> int:
        verify = not self.settings.vcenter_insecure
        host_name = first_writable_host_name(datastore)
        last_error: Optional[BaseException] = None
        if host_name:
            dc_candidates: List[str] = []
            for item in ("ha-datacenter", dc_path):
                if item and item not in dc_candidates:
                    dc_candidates.append(item)
            for dc in dc_candidates:
                url = folder_file_url(host_name, 443, remote, dc, ds_name)
                try:
                    ticket = self._http_put_ticket(url)
                except Exception as exc:
                    last_error = TransientError(f"Could not get ESXi upload ticket for {host_name}: {exc}")
                    break
                if not ticket:
                    continue
                state["phase"] = "esxi_put"
                state["upload_host"] = host_name
                try:
                    return put_file(
                        url,
                        path,
                        start,
                        size,
                        {"Cookie": f"vmware_cgi_ticket={ticket}", "Content-Type": "application/octet-stream"},
                        verify,
                        on_progress,
                        state,
                    )
                except PermanentError as exc:
                    last_error = exc
                    if "404" not in str(exc):
                        raise
                except TransientError as exc:
                    last_error = TransientError(
                        f"Cannot reach ESXi {host_name} for datastore upload (vCenter does not proxy PUTs): {exc}"
                    )
                    break
        url = folder_file_url(self.settings.vcenter_host, self.settings.vcenter_port, remote, dc_path, ds_name)
        cookie = getattr(self._session()._stub, "cookie", "") or ""
        state["phase"] = "vcenter_put"
        try:
            return put_file(
                url,
                path,
                start,
                size,
                {"Cookie": cookie, "Content-Type": "application/octet-stream"},
                verify,
                on_progress,
                state,
            )
        except PermanentError as exc:
            if last_error is not None:
                raise PermanentError(f"{exc}; earlier: {last_error}") from exc
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
