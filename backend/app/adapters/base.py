from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..models import (
    ActionResult,
    CloneVmRequest,
    ConnectionInfo,
    DatastoreListing,
    DatastoreSummary,
    InventorySnapshot,
    MigrateVmRequest,
    NetworkSummary,
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

    def list_datastores(self) -> List[DatastoreSummary]:
        return []

    def list_templates(self) -> List[VmTemplate]:
        return []

    def browse_datastore(self, datastore_id: str, path: str = "") -> DatastoreListing:
        raise NotImplementedError(f"{type(self).__name__} cannot browse datastores")

    def list_networks(self) -> List[NetworkSummary]:
        return []

    def start_clone(self, spec: CloneVmRequest) -> str:
        raise NotImplementedError(f"{type(self).__name__} cannot clone VMs")

    def start_migrate(self, spec: MigrateVmRequest) -> str:
        raise NotImplementedError(f"{type(self).__name__} cannot migrate VMs")

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
