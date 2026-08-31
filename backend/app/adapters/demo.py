from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from ..config import Settings
from ..grouping import resolve_deployed_by, resolve_owner
from ..errors import PermanentError
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
    VirtualDiskSummary,
    VmFolder,
    VmTemplate,
)
from ..reclaim import annotate_idle, build_owner_reports
from ..vm_storage import apply_disk_transform, normalize_disk_transform
from .base import InventoryAdapter

NOW = datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc)
GIB = 1024 ** 3


def _dt(days_ago: Optional[float]) -> Optional[datetime]:
    if days_ago is None:
        return None
    return NOW - timedelta(days=days_ago)


def _demo_storage(vm_id: str, cpu_count: int, guest_os: str) -> tuple[int, int, str]:
    numeric = int(vm_id.split("-")[-1]) if vm_id.split("-")[-1].isdigit() else 0
    windows = "win" in guest_os.lower()
    if numeric in {302, 802}:
        provisioning = "mixed"
    elif windows or numeric % 2:
        provisioning = "thick"
    else:
        provisioning = "thin"
    provisioned = {2: 40, 4: 80, 8: 160, 16: 400}.get(cpu_count, 60) * GIB
    if windows:
        provisioned = max(provisioned, 80 * GIB)
    ratio = {"thin": 0.22, "thick": 0.86, "mixed": 0.48}.get(provisioning, 0.5)
    used = int(provisioned * ratio)
    return used, provisioned, provisioning


class DemoAdapter(InventoryAdapter):
    """Deterministic lab inventory so the UI can be used before vCenter creds exist."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._vms: Dict[str, VirtualMachine] = {}
        self._hosts: List[HostSummary] = []
        self._clusters: List[ClusterSummary] = []
        self._templates: Dict[str, VmTemplate] = {}
        self._datastores: Dict[str, DatastoreSummary] = {}
        self._files: Dict[str, List[DatastoreFile]] = {}
        self._networks: List[NetworkSummary] = []
        self._folders: Dict[str, VmFolder] = {}
        self._drs_overridden: set[str] = set()
        self._maintenance_mode = False
        self._ntp_servers = ["time.cloudflare.com", "time.google.com"]
        self._services = {
            "TSM-SSH": {"label": "SSH", "running": False, "policy": "off", "required": False},
            "ntpd": {"label": "NTP Daemon", "running": True, "policy": "on", "required": False},
        }
        self._seed()

    def connection(self) -> ConnectionInfo:
        capabilities = {
            "inventory": True,
            "power": True,
            "console": False,
            "datastores": True,
            "datastore_browse": True,
            "datastore_write": True,
            "clone": True,
            "migrate": True,
            "disk_convert": True,
            "disk_convert_soap": True,
            "disk_convert_ssh": False,
            "drs": True,
            "permissions": True,
            "migration_roles": True,
            "templates": True,
            "content_library": True,
            "host_admin": True,
            "host_management": True,
            "host_services": True,
            "host_maintenance": True,
            "host_time": True,
            "storage_rescan": True,
            "support_bundle": True,
        }
        return ConnectionInfo(
            mode="demo",
            connected=True,
            host="demo.vcenter.local",
            user="readonly@vsphere.local",
            message="Demo inventory. Sign in from the UI to use a live vCenter.",
            last_sync=NOW,
            endpoint_kind="demo",
            api_type="Demo",
            api_version="demo",
            product_name="vFleet demo",
            product_version="1",
            endpoint_fingerprint="demo",
            capabilities=capabilities,
        )

    def snapshot(self) -> InventorySnapshot:
        vms = []
        for vm in self._vms.values():
            if vm.id in self._templates:
                continue
            copy = vm.model_copy(deep=True)
            if not copy.disks:
                copy.disks = [
                    VirtualDiskSummary(
                        key=2000,
                        label="Hard disk 1",
                        capacity_bytes=copy.storage_provisioned_bytes,
                        file_name=f"[vsan-lab] {copy.name}/{copy.name}.vmdk",
                        datastore_id="ds-vsan",
                        datastore_name="vsan-lab",
                        provisioning=(
                            "thin" if copy.disk_provisioning == "thin" else "lazy_zeroed_thick"
                        ),
                        backing_type="VirtualDiskFlatVer2BackingInfo",
                    )
                ]
            copy = copy.model_copy(update={"drs_override": vm.id in self._drs_overridden})
            vms.append(annotate_idle(copy, self.settings, NOW))
        vms.sort(key=lambda vm: vm.name.lower())
        hosts = [host.model_copy(deep=True) for host in self._hosts]
        for host in hosts:
            host.vm_count = sum(1 for vm in vms if vm.host_id == host.id)
        return InventorySnapshot(
            connection=self.connection(),
            clusters=self._clusters,
            hosts=hosts,
            vms=vms,
            owners=build_owner_reports(vms),
        )

    def apply_actions(self, vm_ids: Iterable[str], action: str) -> List[ActionResult]:
        results: List[ActionResult] = []
        next_state = {
            "start": "POWERED_ON",
            "shutdown": "POWERED_OFF",
            "power_off": "POWERED_OFF",
            "reboot": "POWERED_ON",
            "reset": "POWERED_ON",
            "suspend": "SUSPENDED",
        }.get(action)
        for vm_id in vm_ids:
            vm = self._vms.get(vm_id)
            if vm is None:
                results.append(ActionResult(vm_id=vm_id, name="", action=action, ok=False, message="Unknown VM"))
                continue
            if action == "destroy":
                if vm_id in self._templates:
                    results.append(
                        ActionResult(
                            vm_id=vm_id,
                            name=vm.name,
                            action=action,
                            ok=False,
                            message="Refusing to delete a template",
                        )
                    )
                    continue
                del self._vms[vm_id]
                results.append(
                    ActionResult(vm_id=vm_id, name=vm.name, action=action, ok=True, message="Deleted from disk in demo mode")
                )
                continue
            if action == "mount_tools":
                if vm.power_state != "POWERED_ON":
                    results.append(
                        ActionResult(
                            vm_id=vm_id,
                            name=vm.name,
                            action=action,
                            ok=False,
                            message="Power on the VM before mounting the VMware Tools installer",
                        )
                    )
                else:
                    results.append(
                        ActionResult(
                            vm_id=vm_id,
                            name=vm.name,
                            action=action,
                            ok=True,
                            message="VMware Tools installer mounted in demo mode",
                        )
                    )
                continue
            if next_state is None:
                results.append(
                    ActionResult(vm_id=vm_id, name=vm.name, action=action, ok=False, message="Unsupported action")
                )
                continue
            if action in {"shutdown", "reboot"} and vm.tools_status not in {"toolsOk", "guestToolsRunning"}:
                results.append(
                    ActionResult(
                        vm_id=vm_id,
                        name=vm.name,
                        action=action,
                        ok=False,
                        message="VMware Tools is not running; use power_off or reset instead",
                    )
                )
                continue
            if action == "start" and vm.power_state == "POWERED_ON":
                results.append(
                    ActionResult(vm_id=vm_id, name=vm.name, action=action, ok=False, message="Already powered on")
                )
                continue
            vm.power_state = next_state
            if next_state == "POWERED_ON":
                vm.boot_time = NOW
                vm.last_activity = NOW
                vm.last_activity_source = "power_event"
            elif next_state == "POWERED_OFF":
                vm.cpu_usage_mhz = 0
                vm.cpu_usage_pct = 0
                vm.memory_usage_mib = 0
                vm.memory_usage_pct = 0
                vm.last_activity = NOW
                vm.last_activity_source = "power_event"
            results.append(ActionResult(vm_id=vm_id, name=vm.name, action=action, ok=True, message="Applied in demo mode"))
        return results

    def console_ticket(self, vm_id: str, ticket_type: str = "vmrc") -> ConsoleTicket:
        vm = self._vms.get(vm_id)
        if vm is None:
            raise PermanentError("VM not found")
        raise PermanentError("Console launch requires a live vCenter connection. Use Connect to sign in.")

    def list_datastores(self) -> List[DatastoreSummary]:
        return [item.model_copy(deep=True) for item in self._datastores.values()]

    def historical_metrics(self, snapshot: InventorySnapshot, catalog: Optional[Catalog] = None):
        from ..metrics import HISTORY_DAYS, build_metric_samples, synthesize_history

        return synthesize_history(build_metric_samples(snapshot, catalog or Catalog(datastores=self.list_datastores())), days=HISTORY_DAYS)

    def endpoint_fingerprint(self) -> str:
        return "demo"

    def host_management(self) -> HostManagementInfo:
        host = self._hosts[0]
        return HostManagementInfo(
            host_id=host.id,
            name=host.name,
            endpoint_kind="demo",
            product_name="VMware ESXi (demo)",
            version="8.0.3",
            build="demo",
            api_version="8.0.3.0",
            vendor="VMware",
            model="Nested lab host",
            uuid="demo-host-uuid",
            connection_state=host.connection_state,
            maintenance_mode=self._maintenance_mode,
            uptime_seconds=12 * 86400,
            current_time=NOW,
            ntp_servers=list(self._ntp_servers),
            dns_servers=["10.20.0.10"],
            search_domains=["lab.local"],
            hostname=host.name.split(".")[0],
            domain_name="lab.local",
            license_name="Demo license",
            services=[
                HostServiceSummary(key=key, controllable=True, **value)
                for key, value in self._services.items()
            ],
            storage_adapters=[
                HostStorageAdapterSummary(key="vmhba0", model="Demo NVMe", driver="nvme", status="online", device="vmhba0")
            ],
            health=[HostHealthSensor(name="System health", status="green", reading="Normal")],
            capabilities=self.connection().capabilities,
        )

    def start_host_action(self, action: str, timeout_seconds: int = 900) -> str:
        if action == "maintenance_enter":
            if any(vm.power_state == "POWERED_ON" for vm in self._vms.values() if vm.id not in self._templates):
                raise PermanentError("Power off all virtual machines before entering standalone-host maintenance mode")
            self._maintenance_mode = True
        elif action == "maintenance_exit":
            self._maintenance_mode = False
        elif action in {"reboot", "shutdown"}:
            if not self._maintenance_mode:
                raise PermanentError("Enter maintenance mode before rebooting or shutting down the host")
        else:
            raise PermanentError(f"Unsupported host action: {action}")
        return ""

    def host_service_action(self, service_key: str, action: str, policy: str = "") -> Dict[str, Any]:
        service = self._services.get(service_key)
        if service is None:
            raise PermanentError("Only SSH and NTP services are controllable from vFleet")
        if action == "start":
            service["running"] = True
        elif action == "stop":
            service["running"] = False
        elif action == "restart":
            service["running"] = True
        elif action == "policy" and policy in {"on", "off", "automatic"}:
            service["policy"] = policy
        else:
            raise PermanentError("Unsupported service action")
        return {"service_key": service_key, "action": action, "policy": service["policy"]}

    def configure_host_time(self, ntp_servers: List[str], sync_now: bool = False) -> Dict[str, Any]:
        self._ntp_servers = list(ntp_servers)
        return {"ntp_servers": list(ntp_servers), "synced": bool(sync_now)}

    def rescan_storage(self) -> Dict[str, Any]:
        return {"rescanned": True}

    def start_support_bundle(self) -> str:
        return "demo-support-bundle"

    def disk_conversion_plan(self, spec: DiskConversionPlanRequest) -> DiskConversionPlan:
        target = normalize_disk_transform(spec.target)
        vm = self._vms.get(spec.vm_id)
        if vm is None or vm.id in self._templates:
            raise PermanentError("Unknown VM")
        detailed = "lazy_zeroed_thick" if target == "thick" else target
        disks = self.snapshot().vms[[item.id for item in self.snapshot().vms].index(vm.id)].disks
        noop = all(
            disk.provisioning == detailed
            or (detailed in {"thick", "lazy_zeroed_thick"} and disk.provisioning == "lazy_zeroed_thick")
            for disk in disks
        )
        method = "ssh" if spec.method == "ssh" else "soap"
        blockers = ["SSH fallback is not configured in demo mode"] if method == "ssh" else []
        token = hashlib.sha256(f"demo:{vm.id}:{target}:{method}:{vm.power_state}".encode()).hexdigest()
        return DiskConversionPlan(
            vm_id=vm.id,
            vm_name=vm.name,
            target=target,
            method=method,
            fallback_method="",
            plan_token=token,
            power_state=vm.power_state,
            disks=disks,
            warnings=["Demo plan; no remote disk data is changed"],
            estimated_scratch_bytes=vm.storage_provisioned_bytes,
            noop=noop,
            blockers=blockers,
            can_execute=not noop and not blockers,
        )

    def list_templates(self) -> List[VmTemplate]:
        return [item.model_copy(deep=True) for item in self._templates.values()]

    def list_networks(self) -> List[NetworkSummary]:
        return [item.model_copy(deep=True) for item in self._networks]

    def list_vm_folders(self, datacenter: str = "") -> List[VmFolder]:
        wanted = (datacenter or "").strip().lower()
        rows = list(self._folders.values())
        if wanted:
            rows = [
                item
                for item in rows
                if wanted in {item.datacenter_id.lower(), item.datacenter_name.lower()}
            ]
        rows.sort(key=lambda item: item.path.lower())
        return [item.model_copy(deep=True) for item in rows]

    def _resolve_demo_folder(self, folder_id: str) -> Optional[VmFolder]:
        clean = (folder_id or "").strip()
        if not clean:
            return None
        folder = self._folders.get(clean)
        if folder is None:
            raise PermanentError(f"Unknown VM folder: {clean}")
        return folder

    def browse_datastore(self, datastore_id: str, path: str = "") -> DatastoreListing:
        datastore = self._datastores.get(datastore_id)
        if datastore is None:
            raise KeyError("Unknown datastore")
        prefix = path.strip("/")
        files: List[DatastoreFile] = []
        seen = set()
        for item in self._files.get(datastore_id, []):
            rel = item.path.strip("/")
            if prefix:
                if rel == prefix:
                    continue
                if not rel.startswith(prefix + "/"):
                    continue
                rest = rel[len(prefix) + 1 :]
            else:
                rest = rel
            name = rest.split("/", 1)[0]
            if name in seen:
                continue
            seen.add(name)
            nested = "/" in rest
            files.append(
                DatastoreFile(
                    name=name,
                    path=f"{prefix}/{name}".strip("/"),
                    size=0 if nested else item.size,
                    is_directory=nested or item.is_directory,
                    kind="folder" if nested or item.is_directory else item.kind,
                )
            )
        files.sort(key=lambda row: (not row.is_directory, row.name.lower()))
        return DatastoreListing(datastore_id=datastore_id, datastore_name=datastore.name, path=prefix, files=files)

    def start_clone(self, spec: CloneVmRequest) -> str:
        template = self._templates.get(spec.template_id)
        source = self._vms.get(spec.template_id)
        if template is None or source is None:
            raise KeyError("Unknown template")
        if any(vm.name == spec.name for vm in self._vms.values()):
            raise ValueError(f"VM name already exists: {spec.name}")
        folder = self._resolve_demo_folder(spec.folder_id)
        if folder is None:
            folder = self._folders.get(source.folder_id) or self._folders.get("folder-templates")
        datastore = self._datastores.get(spec.datastore_id)
        host = None
        if spec.host_id:
            host = next((item for item in self._hosts if item.id == spec.host_id), None)
            if host is None:
                raise KeyError("Unknown host")
        else:
            host = next(
                (item for item in self._hosts if not spec.cluster_id or item.cluster_id == spec.cluster_id),
                self._hosts[0],
            )
        vm_id = f"vm-{max(int(key.split('-')[1]) for key in self._vms) + 1}"
        owner, source_label = resolve_owner(spec.name, {}, self.settings.owner_fields, self.settings.name_group_pattern)
        used, provisioned, provisioning = _demo_storage(vm_id, spec.cpu_count or template.cpu_count, template.guest_os)
        self._vms[vm_id] = VirtualMachine(
            id=vm_id,
            name=spec.name,
            power_state="POWERED_ON" if spec.power_on else "POWERED_OFF",
            cpu_count=spec.cpu_count or template.cpu_count,
            memory_mib=spec.memory_mib or template.memory_mib,
            host_id=host.id,
            host_name=host.name,
            cluster_id=host.cluster_id,
            cluster_name=host.cluster_name,
            guest_os=template.guest_os,
            tools_status="toolsNotRunning",
            owner_key=owner,
            owner_source=source_label,
            deployed_by=owner,
            annotation=f"Cloned from {template.name} onto {datastore.name if datastore else spec.datastore_id}",
            last_activity=NOW,
            last_activity_source="vcenter_event",
            storage_used_bytes=used,
            storage_provisioned_bytes=provisioned,
            disk_provisioning=provisioning,
            folder_id=folder.id if folder else "",
            folder_path=folder.path if folder else "",
        )
        return f"demo-clone-{vm_id}"

    def start_clone_migrate(self, spec: CloneMigrateRequest) -> str:
        source = self._vms.get(spec.vm_id)
        if source is None:
            raise KeyError("Unknown VM")
        if source.id in self._templates:
            raise ValueError("Refusing to clone-migrate a template")
        dest = next((item for item in self._hosts if item.id == spec.host_id), None)
        if dest is None:
            raise KeyError("Unknown host")
        if source.cluster_id and dest.cluster_id and source.cluster_id != dest.cluster_id:
            raise PermanentError(f"{source.name} is not in the same cluster as {dest.name}")
        clone_name = (spec.name or "").strip()
        if not clone_name:
            raise ValueError("Clone name is required")
        if any(vm.name == clone_name for vm in self._vms.values()):
            raise ValueError(f"VM name already exists: {clone_name}")
        folder = self._resolve_demo_folder(spec.folder_id)
        if folder is None:
            folder = self._folders.get(source.folder_id) or self._folders.get("folder-vms")
        if spec.datastore_id:
            datastore = self._datastores.get(spec.datastore_id)
            if datastore is None:
                raise KeyError("Unknown datastore")
            if datastore.host_ids and dest.id not in datastore.host_ids:
                raise PermanentError(f"Datastore {datastore.name} is not mounted on the destination host")
        vm_id = f"vm-{max(int(key.split('-')[1]) for key in self._vms) + 1}"
        owner, source_label = resolve_owner(clone_name, {}, self.settings.owner_fields, self.settings.name_group_pattern)
        used, provisioned, provisioning = _demo_storage(vm_id, source.cpu_count, source.guest_os)
        self._vms[vm_id] = VirtualMachine(
            id=vm_id,
            name=clone_name,
            power_state="POWERED_ON" if spec.power_on else "POWERED_OFF",
            cpu_count=source.cpu_count,
            memory_mib=source.memory_mib,
            host_id=dest.id,
            host_name=dest.name,
            cluster_id=dest.cluster_id,
            cluster_name=dest.cluster_name,
            guest_os=source.guest_os,
            tools_status="toolsNotRunning",
            owner_key=owner,
            owner_source=source_label,
            deployed_by=owner or source.deployed_by,
            annotation=f"Clone-migrated from {source.name}",
            last_activity=NOW,
            last_activity_source="vcenter_event",
            storage_used_bytes=used,
            storage_provisioned_bytes=provisioned,
            disk_provisioning=provisioning,
            folder_id=folder.id if folder else source.folder_id,
            folder_path=folder.path if folder else source.folder_path,
        )
        return f"demo-clone-{vm_id}"

    def rename_vm(self, vm_id: str, new_name: str) -> str:
        vm = self._vms.get(vm_id)
        if vm is None:
            raise KeyError("Unknown VM")
        if vm_id in self._templates:
            raise PermanentError("Refusing to rename a template")
        clean = new_name.strip()
        if not clean or "/" in clean:
            raise PermanentError("Invalid VM name")
        if any(item.name == clean and item.id != vm_id for item in self._vms.values()):
            raise PermanentError(f"VM name already exists: {clean}")
        vm.name = clean
        return ""

    def disable_vm_drs(self, vm: Any, cluster_id: str = "", vm_name: str = "", vm_id: str = "") -> str:
        target_id = (vm_id or "").strip()
        if not target_id and vm_name:
            found = next((item for item in self._vms.values() if item.name == vm_name), None)
            target_id = found.id if found is not None else ""
        if not target_id or target_id not in self._vms:
            raise KeyError("Unknown VM")
        if target_id in self._templates:
            raise ValueError("Refusing to change DRS override on a template")
        self._drs_overridden.add(target_id)
        return ""

    def start_migrate(self, spec: MigrateVmRequest) -> str:
        if not spec.vm_ids:
            raise ValueError("No virtual machines selected")
        vm_id = spec.vm_ids[0]
        vm = self._vms.get(vm_id)
        if vm is None:
            raise KeyError("Unknown VM")
        dest = None
        if spec.host_id:
            dest = next((item for item in self._hosts if item.id == spec.host_id), None)
            if dest is None:
                raise KeyError("Unknown host")
            if vm.cluster_id and dest.cluster_id and vm.cluster_id != dest.cluster_id:
                raise PermanentError(f"{vm.name} is not in the same cluster as {dest.name}")
        datastore = None
        if spec.datastore_id:
            datastore = self._datastores.get(spec.datastore_id)
            if datastore is None:
                raise KeyError("Unknown datastore")
            host_id = dest.id if dest is not None else vm.host_id
            if datastore.host_ids and host_id and host_id not in datastore.host_ids:
                raise PermanentError(f"Datastore {datastore.name} is not mounted on the destination host")
        network = None
        if spec.network_id:
            network = next((item for item in self._networks if item.id == spec.network_id), None)
            if network is None:
                raise KeyError("Unknown network")
        try:
            disk = normalize_disk_transform(spec.disk_provisioning)
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc

        changed = False
        notes: List[str] = []
        if dest is not None and dest.id != vm.host_id:
            vm.host_id = dest.id
            vm.host_name = dest.name
            vm.cluster_id = dest.cluster_id
            vm.cluster_name = dest.cluster_name
            notes.append(f"host {dest.name}")
            changed = True
        if datastore is not None:
            notes.append(f"datastore {datastore.name}")
            changed = True
        if network is not None:
            notes.append(f"network {network.name}")
            changed = True
        if disk:
            used, kind = apply_disk_transform(vm.storage_provisioned_bytes, disk)
            vm.disk_provisioning = kind
            vm.storage_used_bytes = used
            notes.append(f"{kind} disks")
            changed = True
        if not changed:
            return ""
        vm.last_activity = NOW
        vm.last_activity_source = "vcenter_event"
        if notes:
            vm.annotation = "Migrated: " + ", ".join(notes)
        return f"demo-migrate-{vm.id}"

    def start_move_into_folder(self, vm_id: str, folder_id: str) -> str:
        vm = self._vms.get(vm_id)
        if vm is None:
            raise KeyError("Unknown VM")
        folder = self._resolve_demo_folder(folder_id)
        if folder is None:
            return ""
        if vm.folder_id == folder.id:
            return ""
        vm.folder_id = folder.id
        vm.folder_path = folder.path
        vm.last_activity = NOW
        vm.last_activity_source = "vcenter_event"
        return f"demo-folder-{vm.id}"

    def migration_access(self, cluster_id: str = "") -> dict:
        from ..migration_access import ROLE_NAME, privilege_rows

        cluster = next((item for item in self._clusters if item.id == cluster_id), self._clusters[0])
        granted = {row["id"]: True for row in privilege_rows()}
        return {
            "principal": "readonly@vsphere.local",
            "scope": "cluster",
            "cluster_id": cluster.id if cluster else cluster_id,
            "cluster_name": cluster.name if cluster else "",
            "role_name": ROLE_NAME,
            "can_modify_permissions": True,
            "privileges": privilege_rows(granted),
            "message": "Demo mode: migrate privileges are already granted.",
        }

    def grant_migration_access(self, cluster_id: str = "", scope: str = "cluster") -> dict:
        status = self.migration_access(cluster_id)
        status["scope"] = scope if scope in {"cluster", "global"} else "cluster"
        status["message"] = (
            f"Demo: assigned {status['role_name']} to {status['principal']} "
            f"on {'root (global)' if scope == 'global' else status['cluster_name']}."
        )
        return status

    def mkdir(self, datastore_id: str, path: str) -> None:
        if datastore_id not in self._datastores:
            raise KeyError("Unknown datastore")
        clean = path.strip("/")
        self._files.setdefault(datastore_id, []).append(
            DatastoreFile(name=clean.split("/")[-1], path=clean, is_directory=True, kind="folder")
        )

    def delete_file(self, datastore_id: str, path: str) -> None:
        clean = path.strip("/")
        rows = self._files.get(datastore_id, [])
        self._files[datastore_id] = [row for row in rows if row.path != clean and not row.path.startswith(clean + "/")]

    def stat_file(self, datastore_id: str, path: str) -> Optional[int]:
        clean = path.strip("/")
        for row in self._files.get(datastore_id, []):
            if row.path == clean and not row.is_directory:
                return row.size
        return None

    def upload_file(
        self,
        datastore_id: str,
        remote_path: str,
        local_path: str,
        extra: Optional[Dict] = None,
        on_progress=None,
        use_library: bool = True,
    ) -> Dict:
        from pathlib import Path

        if datastore_id not in self._datastores:
            raise KeyError("Unknown datastore")
        if self._datastores[datastore_id].readonly:
            raise PermanentError(f"Datastore {self._datastores[datastore_id].name} is mounted read-only")
        size = Path(local_path).stat().st_size if Path(local_path).exists() else 0
        if on_progress:
            on_progress(size, extra or {})
        clean = remote_path.strip("/")
        name = clean.split("/")[-1]
        kind = "iso" if name.lower().endswith(".iso") else "file"
        rows = [row for row in self._files.get(datastore_id, []) if row.path != clean]
        rows.append(DatastoreFile(name=name, path=clean, size=size, kind=kind))
        self._files[datastore_id] = rows
        state = dict(extra or {})
        state["bytes_sent"] = size
        return state

    def _seed(self) -> None:
        clusters = [
            ClusterSummary(
                id="cluster-lab",
                name="Lab-Cluster",
                host_count=3,
                vm_count=0,
                cpu_cores=96,
                cpu_usage_mhz=41200,
                cpu_capacity_mhz=249600,
                cpu_usage_pct=16.5,
                memory_mib=786432,
                memory_usage_mib=214000,
                memory_usage_pct=27.2,
            ),
            ClusterSummary(
                id="cluster-shared",
                name="Shared-Services",
                host_count=2,
                vm_count=0,
                cpu_cores=64,
                cpu_usage_mhz=28800,
                cpu_capacity_mhz=166400,
                cpu_usage_pct=17.3,
                memory_mib=524288,
                memory_usage_mib=198000,
                memory_usage_pct=37.8,
            ),
        ]
        hosts = [
            HostSummary(
                id="host-esx01",
                name="esx01.lab.local",
                cluster_id="cluster-lab",
                cluster_name="Lab-Cluster",
                connection_state="CONNECTED",
                power_state="POWERED_ON",
                cpu_cores=32,
                cpu_mhz=2600,
                cpu_usage_mhz=14800,
                cpu_usage_pct=17.8,
                memory_mib=262144,
                memory_usage_mib=88000,
                memory_usage_pct=33.6,
            ),
            HostSummary(
                id="host-esx02",
                name="esx02.lab.local",
                cluster_id="cluster-lab",
                cluster_name="Lab-Cluster",
                connection_state="CONNECTED",
                power_state="POWERED_ON",
                cpu_cores=32,
                cpu_mhz=2600,
                cpu_usage_mhz=13200,
                cpu_usage_pct=15.9,
                memory_mib=262144,
                memory_usage_mib=72000,
                memory_usage_pct=27.5,
            ),
            HostSummary(
                id="host-esx03",
                name="esx03.lab.local",
                cluster_id="cluster-lab",
                cluster_name="Lab-Cluster",
                connection_state="CONNECTED",
                power_state="POWERED_ON",
                cpu_cores=32,
                cpu_mhz=2600,
                cpu_usage_mhz=13200,
                cpu_usage_pct=15.9,
                memory_mib=262144,
                memory_usage_mib=54000,
                memory_usage_pct=20.6,
            ),
            HostSummary(
                id="host-esx10",
                name="esx10.shared.local",
                cluster_id="cluster-shared",
                cluster_name="Shared-Services",
                connection_state="CONNECTED",
                power_state="POWERED_ON",
                cpu_cores=32,
                cpu_mhz=2600,
                cpu_usage_mhz=16400,
                cpu_usage_pct=19.7,
                memory_mib=262144,
                memory_usage_mib=110000,
                memory_usage_pct=42.0,
            ),
            HostSummary(
                id="host-esx11",
                name="esx11.shared.local",
                cluster_id="cluster-shared",
                cluster_name="Shared-Services",
                connection_state="CONNECTED",
                power_state="POWERED_ON",
                cpu_cores=32,
                cpu_mhz=2600,
                cpu_usage_mhz=12400,
                cpu_usage_pct=14.9,
                memory_mib=262144,
                memory_usage_mib=88000,
                memory_usage_pct=33.6,
            ),
        ]

        rows = [
            # id, name, power, cpus, mem, cpu_mhz, cpu_pct, mem_used, mem_pct, host, guest, tools, ip, boot_days, activity_days, field_name, field_value, deployed_by
            ("vm-101", "mzipkin-win11-lab", "POWERED_ON", 4, 16384, 2100, 42, 9200, 56, "host-esx01", "windows2019Guest", "toolsOk", "10.12.4.21", 2, 0.4, "Owner", "mzipkin", ""),
            ("vm-102", "mzipkin-ubuntu-dev", "POWERED_ON", 2, 8192, 380, 8, 2100, 26, "host-esx01", "ubuntu64Guest", "toolsOk", "10.12.4.22", 18, 16, "Owner", "mzipkin", ""),
            ("vm-103", "mzipkin-k8s-cp", "POWERED_OFF", 4, 8192, 0, 0, 0, 0, "host-esx02", "ubuntu64Guest", "toolsNotRunning", None, None, 40, "Owner", "mzipkin", ""),
            ("vm-104", "mzipkin-win-sql", "POWERED_ON", 8, 32768, 420, 3, 1800, 5, "host-esx02", "windows2019Guest", "toolsOk", "10.12.4.40", 47, 45, "Owner", "mzipkin", ""),
            ("vm-201", "jdoe-rhel-lab01", "POWERED_ON", 4, 16384, 6400, 78, 11000, 67, "host-esx01", "rhel9_64Guest", "toolsOk", "10.12.5.11", 1, 0.2, "", "", "CORP\\jdoe"),
            ("vm-202", "jdoe-rhel-lab02", "POWERED_ON", 4, 16384, 120, 2, 900, 5, "host-esx03", "rhel9_64Guest", "toolsOk", "10.12.5.12", 22, 21, "", "", "CORP\\jdoe"),
            ("vm-203", "jdoe-win11-test", "SUSPENDED", 2, 8192, 0, 0, 0, 0, "host-esx03", "windows9_64Guest", "toolsNotRunning", None, 9, 9, "", "", "CORP\\jdoe"),
            ("vm-301", "achen-ml-gpu01", "POWERED_ON", 16, 65536, 9800, 31, 42000, 64, "host-esx10", "ubuntu64Guest", "toolsOk", "10.20.1.8", 3, 1.1, "User", "achen", ""),
            ("vm-302", "achen-ml-gpu02", "POWERED_ON", 16, 65536, 400, 1, 2400, 4, "host-esx10", "ubuntu64Guest", "toolsOk", "10.20.1.9", 61, 58, "User", "achen", ""),
            ("vm-303", "achen-notebook", "POWERED_OFF", 4, 16384, 0, 0, 0, 0, "host-esx11", "ubuntu64Guest", "toolsNotRunning", None, None, 12, "User", "achen", ""),
            ("vm-401", "rpatel-ad-dc01", "POWERED_ON", 2, 4096, 520, 14, 1800, 44, "host-esx11", "windows2019Guest", "toolsOk", "10.20.8.10", 4, 1, "", "", "rpatel"),
            ("vm-402", "rpatel-ad-dc02", "POWERED_ON", 2, 4096, 80, 2, 700, 17, "host-esx11", "windows2019Guest", "toolsOk", "10.20.8.11", 4, 3, "", "", "rpatel"),
            ("vm-403", "rpatel-win11-jump", "POWERED_ON", 2, 8192, 90, 3, 1100, 13, "host-esx02", "windows9_64Guest", "toolsOk", "10.12.8.50", 29, 27, "", "", "rpatel"),
            ("vm-551", "windows-lab-shared", "POWERED_ON", 4, 16384, 800, 8, 4200, 26, "host-esx01", "windows9_64Guest", "toolsOk", "10.12.8.90", 6, 5, "", "", "pbogar"),
            ("vm-552", "vm-debug-analyzer", "POWERED_ON", 2, 8192, 220, 4, 1600, 19, "host-esx02", "ubuntu64Guest", "toolsOk", "10.12.8.91", 2, 1, "", "", "skakade"),
            ("vm-601", "shared-nexus-proxy", "POWERED_ON", 4, 8192, 2100, 28, 4300, 52, "host-esx10", "ubuntu64Guest", "toolsOk", "10.20.0.15", 0.5, 0.1, "Owner", "platform", ""),
            ("vm-602", "shared-dns01", "POWERED_ON", 2, 2048, 180, 6, 700, 34, "host-esx11", "ubuntu64Guest", "toolsOk", "10.20.0.10", 0.8, 0.2, "Owner", "platform", ""),
            ("vm-701", "kwong-kali-lab", "POWERED_ON", 2, 8192, 60, 2, 600, 7, "host-esx02", "ubuntu64Guest", "toolsOk", "10.12.9.70", 38, 37, "", "", "kwong"),
            ("vm-702", "kwong-win-lab", "POWERED_ON", 4, 16384, 110, 2, 800, 5, "host-esx01", "windows9_64Guest", "toolsNotRunning", None, 51, 51, "", "", "kwong"),
            ("vm-801", "slee-ubuntu-course", "POWERED_OFF", 2, 4096, 0, 0, 0, 0, "host-esx03", "ubuntu64Guest", "toolsNotRunning", None, None, 8, "", "", "slee"),
            ("vm-802", "slee-docker-lab", "POWERED_ON", 8, 32768, 240, 2, 1500, 5, "host-esx02", "ubuntu64Guest", "toolsOk", "10.12.9.81", 19, 19, "", "", "slee"),
        ]

        host_by_id = {host.id: host for host in hosts}
        folders = [
            VmFolder(
                id="folder-vms",
                name="vm",
                path="Lab-DC / vm",
                parent_id="",
                datacenter_id="dc-lab",
                datacenter_name="Lab-DC",
            ),
            VmFolder(
                id="folder-playground",
                name="SHARED-Playground",
                path="Lab-DC / vm / SHARED-Playground",
                parent_id="folder-vms",
                datacenter_id="dc-lab",
                datacenter_name="Lab-DC",
            ),
            VmFolder(
                id="folder-support",
                name="Support-Team",
                path="Lab-DC / vm / Support-Team",
                parent_id="folder-vms",
                datacenter_id="dc-lab",
                datacenter_name="Lab-DC",
            ),
            VmFolder(
                id="folder-training",
                name="Training-Team",
                path="Lab-DC / vm / Training-Team",
                parent_id="folder-vms",
                datacenter_id="dc-lab",
                datacenter_name="Lab-DC",
            ),
            VmFolder(
                id="folder-templates",
                name="VM Templates",
                path="Lab-DC / vm / VM Templates",
                parent_id="folder-vms",
                datacenter_id="dc-lab",
                datacenter_name="Lab-DC",
            ),
        ]
        self._folders = {item.id: item for item in folders}
        playground = self._folders["folder-playground"]
        support = self._folders["folder-support"]
        training = self._folders["folder-training"]

        vms: Dict[str, VirtualMachine] = {}
        for row in rows:
            (
                vm_id,
                name,
                power,
                cpus,
                mem,
                cpu_mhz,
                cpu_pct,
                mem_used,
                mem_pct,
                host_id,
                guest,
                tools,
                ip,
                boot_days,
                activity_days,
                field_name,
                field_value,
                event_user,
            ) = row
            host = host_by_id[host_id]
            fields = {field_name: field_value} if field_name and field_value else {}
            owner, source = resolve_owner(name, fields, self.settings.owner_fields, self.settings.name_group_pattern)
            source_label = "custom_field" if source == "custom_field" else "name_prefix"
            deployed_by = resolve_deployed_by(fields, self.settings.owner_fields, event_user)
            activity_source = "console_event" if activity_days is not None and activity_days < 5 else "power_event"
            if activity_days is None:
                activity_source = "unknown"
            used, provisioned, provisioning = _demo_storage(vm_id, cpus, guest)
            if host.cluster_id == "cluster-shared":
                folder = support
            elif owner.startswith("slee") or owner.startswith("kwong"):
                folder = training
            else:
                folder = playground
            vms[vm_id] = VirtualMachine(
                id=vm_id,
                name=name,
                power_state=power,
                cpu_count=cpus,
                memory_mib=mem,
                cpu_usage_mhz=cpu_mhz,
                cpu_usage_pct=cpu_pct,
                memory_usage_mib=mem_used,
                memory_usage_pct=mem_pct,
                host_id=host.id,
                host_name=host.name,
                cluster_id=host.cluster_id,
                cluster_name=host.cluster_name,
                guest_os=guest,
                tools_status=tools,
                ip_address=ip,
                boot_time=_dt(boot_days),
                last_activity=_dt(activity_days),
                last_activity_source=activity_source,
                owner_key=owner,
                owner_source=source_label,
                deployed_by=deployed_by,
                custom_fields=fields,
                annotation="Demo fixture",
                storage_used_bytes=used,
                storage_provisioned_bytes=provisioned,
                disk_provisioning=provisioning,
                folder_id=folder.id,
                folder_path=folder.path,
            )

        for cluster in clusters:
            cluster.vm_count = sum(1 for vm in vms.values() if vm.cluster_id == cluster.id)
            cluster.host_count = sum(1 for host in hosts if host.cluster_id == cluster.id)

        self._clusters = clusters
        self._hosts = hosts
        self._vms = vms

        lab_hosts = ["host-esx01", "host-esx02", "host-esx03"]
        shared_hosts = ["host-esx10", "host-esx11"]
        all_hosts = lab_hosts + shared_hosts
        datastores = [
            DatastoreSummary(
                id="ds-iso",
                name="ISO",
                type="VMFS",
                capacity_bytes=2_199_023_255_552,
                free_bytes=1_288_490_188_800,
                datacenter="Lab-DC",
                datacenter_path="Lab-DC",
                host_count=5,
                host_ids=list(all_hosts),
                usage_pct=41.4,
            ),
            DatastoreSummary(
                id="ds-lab",
                name="Lab-SAS",
                type="VMFS",
                capacity_bytes=8_796_093_022_208,
                free_bytes=3_221_225_472_000,
                datacenter="Lab-DC",
                datacenter_path="Lab-DC",
                host_count=3,
                host_ids=list(lab_hosts),
                usage_pct=63.4,
            ),
            DatastoreSummary(
                id="ds-nfs",
                name="NFS-Templates",
                type="NFS",
                capacity_bytes=4_398_046_511_104,
                free_bytes=2_792_647_499_776,
                datacenter="Lab-DC",
                datacenter_path="Lab-DC",
                host_count=5,
                host_ids=list(all_hosts),
                usage_pct=36.5,
            ),
        ]
        self._datastores = {item.id: item for item in datastores}
        self._networks = [
            NetworkSummary(
                id="net-vm",
                name="VM Network",
                type="standard",
                host_count=5,
                host_ids=list(all_hosts),
            ),
            NetworkSummary(
                id="net-lab",
                name="Lab-VMs",
                type="distributed",
                host_count=3,
                host_ids=list(lab_hosts),
            ),
            NetworkSummary(
                id="net-shared",
                name="Shared-VMs",
                type="distributed",
                host_count=2,
                host_ids=list(shared_hosts),
            ),
        ]
        self._files = {
            "ds-iso": [
                DatastoreFile(name="isos", path="isos", is_directory=True, kind="folder"),
                DatastoreFile(name="windows11.iso", path="isos/windows11.iso", size=5_368_709_120, kind="iso"),
                DatastoreFile(name="rhel9.iso", path="isos/rhel9.iso", size=8_589_934_592, kind="iso"),
                DatastoreFile(name="tools", path="tools", is_directory=True, kind="folder"),
                DatastoreFile(name="vmtools.iso", path="tools/vmtools.iso", size=156_237_824, kind="iso"),
            ],
            "ds-lab": [
                DatastoreFile(name="mzipkin-win11-lab", path="mzipkin-win11-lab", is_directory=True, kind="folder"),
                DatastoreFile(name="uploads", path="uploads", is_directory=True, kind="folder"),
            ],
            "ds-nfs": [
                DatastoreFile(name="win11-gold", path="win11-gold", is_directory=True, kind="folder"),
                DatastoreFile(name="rhel9-gold", path="rhel9-gold", is_directory=True, kind="folder"),
            ],
        }
        self._templates = {
            "vm-501": VmTemplate(
                id="vm-501",
                name="win11-gold",
                cpu_count=4,
                memory_mib=8192,
                guest_os="windows9_64Guest",
                cluster_id="cluster-lab",
                cluster_name="Lab-Cluster",
                datastore_id="ds-nfs",
                datastore_name="NFS-Templates",
            ),
            "vm-502": VmTemplate(
                id="vm-502",
                name="rhel9-gold",
                cpu_count=4,
                memory_mib=8192,
                guest_os="rhel9_64Guest",
                cluster_id="cluster-lab",
                cluster_name="Lab-Cluster",
                datastore_id="ds-nfs",
                datastore_name="NFS-Templates",
            ),
        }
        host = host_by_id["host-esx03"]
        template_folder = self._folders["folder-templates"]
        for template in self._templates.values():
            owner, source_label = resolve_owner(template.name, {}, self.settings.owner_fields, self.settings.name_group_pattern)
            used, provisioned, provisioning = _demo_storage(template.id, template.cpu_count, template.guest_os)
            vms[template.id] = VirtualMachine(
                id=template.id,
                name=template.name,
                power_state="POWERED_OFF",
                cpu_count=template.cpu_count,
                memory_mib=template.memory_mib,
                host_id=host.id,
                host_name=host.name,
                cluster_id=host.cluster_id,
                cluster_name=host.cluster_name,
                guest_os=template.guest_os,
                tools_status="toolsNotRunning",
                owner_key=owner,
                owner_source=source_label,
                deployed_by=owner,
                annotation="Demo template",
                storage_used_bytes=used,
                storage_provisioned_bytes=provisioned,
                disk_provisioning=provisioning,
                folder_id=template_folder.id,
                folder_path=template_folder.path,
            )
