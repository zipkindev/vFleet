from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..models import (
    ActionResult,
    Catalog,
    CloneMigrateRequest,
    CloneVmRequest,
    ConnectionInfo,
    ConsoleTicket,
    DatastoreListing,
    DatastoreSummary,
    DiskConversionPlan,
    DiskConversionPlanRequest,
    HostManagementInfo,
    InventorySnapshot,
    MigrateVmRequest,
    NetworkSummary,
    VmFolder,
    VmTemplate,
)

ProgressFn = Callable[[int, Dict[str, Any]], None]


class InventoryAdapter(ABC):
    @abstractmethod
    def connection(self) -> ConnectionInfo:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self) -> InventorySnapshot:
        raise NotImplementedError

    @abstractmethod
    def apply_actions(self, vm_ids: Iterable[str], action: str) -> List[ActionResult]:
        raise NotImplementedError

    def console_ticket(self, vm_id: str, ticket_type: str = "vmrc") -> ConsoleTicket:
        raise NotImplementedError(f"{type(self).__name__} cannot launch a VM console")

    def list_datastores(self) -> List[DatastoreSummary]:
        return []

    def list_templates(self) -> List[VmTemplate]:
        return []

    def browse_datastore(self, datastore_id: str, path: str = "") -> DatastoreListing:
        raise NotImplementedError(f"{type(self).__name__} cannot browse datastores")

    def list_networks(self) -> List[NetworkSummary]:
        return []

    def list_vm_folders(self, datacenter: str = "") -> List[VmFolder]:
        return []

    def start_clone(self, spec: CloneVmRequest) -> str:
        raise NotImplementedError(f"{type(self).__name__} cannot clone VMs")

    def start_clone_migrate(self, spec: CloneMigrateRequest) -> str:
        raise NotImplementedError(f"{type(self).__name__} cannot clone-migrate VMs")

    def rename_vm(self, vm_id: str, new_name: str) -> str:
        raise NotImplementedError(f"{type(self).__name__} cannot rename VMs")

    def disable_vm_drs(self, vm: Any, cluster_id: str = "", vm_name: str = "", vm_id: str = "") -> str:
        return ""

    def start_migrate(self, spec: MigrateVmRequest) -> str:
        raise NotImplementedError(f"{type(self).__name__} cannot migrate VMs")

    def start_move_into_folder(self, vm_id: str, folder_id: str) -> str:
        raise NotImplementedError(f"{type(self).__name__} cannot move VMs into folders")

    def migration_access(self, cluster_id: str = "") -> Dict[str, Any]:
        from ..migration_access import ROLE_NAME, privilege_rows

        return {
            "principal": "",
            "scope": "cluster",
            "cluster_id": cluster_id,
            "cluster_name": "",
            "role_name": ROLE_NAME,
            "can_modify_permissions": False,
            "privileges": privilege_rows(),
            "message": f"{type(self).__name__} cannot inspect vCenter roles",
        }

    def grant_migration_access(self, cluster_id: str = "", scope: str = "cluster") -> Dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__} cannot grant vCenter permissions")

    def wait_task(self, task_id: str) -> Any:
        return None

    def historical_metrics(self, snapshot: InventorySnapshot, catalog: Optional[Catalog] = None):
        return []

    def ping(self) -> None:
        return None

    def endpoint_fingerprint(self) -> str:
        return self.connection().endpoint_fingerprint

    def host_management(self) -> HostManagementInfo:
        raise NotImplementedError(f"{type(self).__name__} cannot inspect host management settings")

    def start_host_action(self, action: str, timeout_seconds: int = 900) -> str:
        raise NotImplementedError(f"{type(self).__name__} cannot manage hosts")

    def host_service_action(self, service_key: str, action: str, policy: str = "") -> Dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__} cannot manage host services")

    def configure_host_time(self, ntp_servers: List[str], sync_now: bool = False) -> Dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__} cannot configure host time")

    def rescan_storage(self) -> Dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__} cannot rescan host storage")

    def start_support_bundle(self) -> str:
        raise NotImplementedError(f"{type(self).__name__} cannot generate a support bundle")

    def disk_conversion_plan(self, spec: DiskConversionPlanRequest) -> DiskConversionPlan:
        raise NotImplementedError(f"{type(self).__name__} cannot plan disk conversion")

    def execute_ssh_disk_conversion(
        self,
        plan: DiskConversionPlan,
        on_progress: Optional[ProgressFn] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__} cannot convert disks over SSH")

    def mkdir(self, datastore_id: str, path: str) -> None:
        raise NotImplementedError(f"{type(self).__name__} cannot create datastore folders")

    def delete_file(self, datastore_id: str, path: str) -> None:
        raise NotImplementedError(f"{type(self).__name__} cannot delete datastore files")

    def stat_file(self, datastore_id: str, path: str) -> Optional[int]:
        return None

    def upload_file(
        self,
        datastore_id: str,
        remote_path: str,
        local_path: str,
        extra: Optional[Dict[str, Any]] = None,
        on_progress: Optional[ProgressFn] = None,
        use_library: bool = True,
    ) -> Dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__} cannot upload files")
