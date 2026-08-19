from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConnectionInfo(BaseModel):
    mode: str
    connected: bool
    host: str = ""
    user: str = ""
    message: str = ""
    last_sync: Optional[datetime] = None
    source: str = "demo"
    env_ready: bool = False
    saved_host: str = ""
    saved_user: str = ""
    saved_port: int = 443
    insecure: bool = True
    can_disconnect: bool = False
    stale: bool = False
    syncing: bool = False
    last_error: str = ""
    queued_jobs: int = 0
    active_jobs: int = 0
    cache_age_seconds: Optional[float] = None


class LoginRequest(BaseModel):
    host: str
    user: str
    password: str
    port: int = 443
    insecure: bool = True
    remember: bool = False
    connect: bool = True


class LogoutRequest(BaseModel):
    forget: bool = False


class HostSummary(BaseModel):
    id: str
    name: str
    cluster_id: str
    cluster_name: str
    connection_state: str
    power_state: str
    cpu_cores: int
    cpu_mhz: int
    cpu_usage_mhz: int
    cpu_usage_pct: float
    memory_mib: int
    memory_usage_mib: int
    memory_usage_pct: float
    vm_count: int = 0


class ClusterSummary(BaseModel):
    id: str
    name: str
    host_count: int
    vm_count: int
    cpu_cores: int
    cpu_usage_mhz: int
    cpu_capacity_mhz: int
    cpu_usage_pct: float
    memory_mib: int
    memory_usage_mib: int
    memory_usage_pct: float


class VirtualMachine(BaseModel):
    id: str
    name: str
    power_state: str
    cpu_count: int
    memory_mib: int
    cpu_usage_mhz: int = 0
    cpu_usage_pct: float = 0.0
    memory_usage_mib: int = 0
    memory_usage_pct: float = 0.0
    host_id: str = ""
    host_name: str = ""
    cluster_id: str = ""
    cluster_name: str = ""
    guest_os: str = ""
    tools_status: str = ""
    ip_address: Optional[str] = None
    boot_time: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    last_activity_source: str = "unknown"
    days_idle: Optional[float] = None
    owner_key: str
    owner_source: str
    custom_fields: Dict[str, str] = Field(default_factory=dict)
    idle_score: int = 0
    reclaim_reason: Optional[str] = None
    annotation: str = ""
    storage_used_bytes: int = 0
    storage_provisioned_bytes: int = 0
    disk_provisioning: str = "unknown"


class OwnerReport(BaseModel):
    owner_key: str
    owner_source: str
    vm_count: int
    powered_on: int
    powered_off: int
    suspended: int
    cpu_count: int
    memory_mib: int
    cpu_usage_mhz: int
    memory_usage_mib: int
    idle_candidates: int
    reclaimable_memory_mib: int
    vms: List[str] = Field(default_factory=list)


class InventorySnapshot(BaseModel):
    connection: ConnectionInfo
    clusters: List[ClusterSummary]
    hosts: List[HostSummary]
    vms: List[VirtualMachine]
    owners: List[OwnerReport]


class ActionRequest(BaseModel):
    vm_ids: List[str]
    action: str
    confirm: bool = False


class ActionResult(BaseModel):
    vm_id: str
    name: str
    action: str
    ok: bool
    message: str


class ActionResponse(BaseModel):
    results: List[ActionResult]
    job_id: Optional[str] = None
    queued: bool = False


class DatastoreSummary(BaseModel):
    id: str
    name: str
    type: str = ""
    capacity_bytes: int = 0
    free_bytes: int = 0
    accessible: bool = True
    datacenter: str = ""
    datacenter_path: str = ""
    host_count: int = 0
    host_ids: List[str] = Field(default_factory=list)
    usage_pct: float = 0.0


class NetworkSummary(BaseModel):
    id: str
    name: str
    type: str = "standard"
    accessible: bool = True
    host_count: int = 0
    host_ids: List[str] = Field(default_factory=list)


class DatastoreFile(BaseModel):
    name: str
    path: str
    size: int = 0
    is_directory: bool = False
    modified: Optional[datetime] = None
    kind: str = "file"


class DatastoreListing(BaseModel):
    datastore_id: str
    datastore_name: str
    path: str
    files: List[DatastoreFile] = Field(default_factory=list)
    stale: bool = False


class VmTemplate(BaseModel):
    id: str
    name: str
    cpu_count: int = 0
    memory_mib: int = 0
    guest_os: str = ""
    cluster_id: str = ""
    cluster_name: str = ""
    datastore_id: str = ""
    datastore_name: str = ""


class Catalog(BaseModel):
    datastores: List[DatastoreSummary] = Field(default_factory=list)
    templates: List[VmTemplate] = Field(default_factory=list)
    networks: List[NetworkSummary] = Field(default_factory=list)
    stale: bool = False
    last_sync: Optional[datetime] = None


class CloneVmRequest(BaseModel):
    template_id: str
    name: str
    datastore_id: str
    cluster_id: str = ""
    cpu_count: Optional[int] = None
    memory_mib: Optional[int] = None
    power_on: bool = False


class MigrateVmRequest(BaseModel):
    vm_ids: List[str]
    host_id: str = ""
    datastore_id: str = ""
    network_id: str = ""
    disk_provisioning: str = ""
    confirm: bool = False


class MkdirRequest(BaseModel):
    datastore_id: str
    path: str


class DeleteFileRequest(BaseModel):
    datastore_id: str
    path: str
    confirm: bool = False


class StagingCreateRequest(BaseModel):
    filename: str
    size: int


class StagingSession(BaseModel):
    id: str
    filename: str
    size: int
    received: int = 0
    complete: bool = False


class UploadRequest(BaseModel):
    staging_id: str
    datastore_id: str
    remote_path: str
    use_library: bool = True


class Job(BaseModel):
    id: str
    kind: str
    title: str
    status: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    progress: Dict[str, Any] = Field(default_factory=dict)
    result: Dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    attempts: int = 0
    max_attempts: int = 80
    next_run_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    idempotency_key: str = ""


class JobList(BaseModel):
    jobs: List[Job]
    queued: int = 0
    active: int = 0


class PrivilegeCheck(BaseModel):
    id: str
    label: str
    granted: bool = False


class MigrationAccessStatus(BaseModel):
    principal: str = ""
    scope: str = "cluster"
    cluster_id: str = ""
    cluster_name: str = ""
    role_name: str = "vFleet-Migrate"
    can_modify_permissions: bool = False
    privileges: List[PrivilegeCheck] = Field(default_factory=list)
    message: str = ""


class GrantMigrationRequest(BaseModel):
    confirm: bool = False
    scope: str = "cluster"
    cluster_id: str = ""
    job_id: str = ""


class MetricPoint(BaseModel):
    ts: datetime
    cpu_pct: float
    memory_pct: float
    disk_pct: float
    cpu_usage_mhz: int = 0
    memory_usage_mib: int = 0
    storage_bytes: int = 0


class OwnerUtilization(BaseModel):
    owner_key: str
    vm_count: int = 0
    cpu_usage_mhz: int = 0
    memory_usage_mib: int = 0
    storage_bytes: int = 0
    cpu_share_pct: float = 0.0
    memory_share_pct: float = 0.0
    storage_share_pct: float = 0.0
    cpu_usage_pct: float = 0.0
    memory_usage_pct: float = 0.0


class MetricsResponse(BaseModel):
    series: List[MetricPoint]
    owners: List[OwnerUtilization]
    hours: float
    owner: str = ""
    host: str = ""
    datastore: str = ""
    points: int = 0
