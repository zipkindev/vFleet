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
    has_saved_password: bool = False
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
    endpoint_kind: str = "demo"
    api_type: str = ""
    api_version: str = ""
    product_name: str = ""
    product_version: str = ""
    product_build: str = ""
    instance_uuid: str = ""
    endpoint_fingerprint: str = ""
    capabilities: Dict[str, bool] = Field(default_factory=dict)
    ssh_configured: bool = False


class LoginRequest(BaseModel):
    host: str
    user: str
    password: str = ""
    port: int = 443
    insecure: bool = True
    remember: bool = False
    connect: bool = True
    endpoint_kind: str = "auto"
    ssh_enabled: bool = False
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_port: int = 22
    ssh_host_key_sha256: str = ""


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
    maintenance_mode: bool = False
    uptime_seconds: int = 0
    vendor: str = ""
    model: str = ""


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
    deployed_by: str = ""
    custom_fields: Dict[str, str] = Field(default_factory=dict)
    idle_score: int = 0
    reclaim_reason: Optional[str] = None
    annotation: str = ""
    storage_used_bytes: int = 0
    storage_provisioned_bytes: int = 0
    disk_provisioning: str = "unknown"
    drs_override: bool = False
    folder_id: str = ""
    folder_path: str = ""
    disks: List["VirtualDiskSummary"] = Field(default_factory=list)


class VirtualDiskSummary(BaseModel):
    key: int
    label: str = ""
    capacity_bytes: int = 0
    file_name: str = ""
    datastore_id: str = ""
    datastore_name: str = ""
    provisioning: str = "unknown"
    disk_mode: str = ""
    backing_type: str = ""
    parent_depth: int = 0
    rdm: bool = False
    encrypted: bool = False
    sharing: str = ""


class VmFolder(BaseModel):
    id: str
    name: str
    path: str
    parent_id: str = ""
    datacenter_id: str = ""
    datacenter_name: str = ""


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


class ConsoleTicketRequest(BaseModel):
    type: str = "vmrc"


class ConsoleTicket(BaseModel):
    vm_id: str
    name: str
    type: str
    uri: str = ""
    host: str = ""
    port: int = 0
    ticket: str = ""
    ssl_thumbprint: str = ""
    vcenter_url: str = ""
    expires_in_seconds: int = 1800
    message: str = ""


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
    readonly: bool = False


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
    host_id: str = ""
    folder_id: str = ""
    disable_drs: bool = False
    cpu_count: Optional[int] = None
    memory_mib: Optional[int] = None
    power_on: bool = False


DeployVmRequest = CloneVmRequest


class MigrateVmRequest(BaseModel):
    vm_ids: List[str]
    host_id: str = ""
    datastore_id: str = ""
    network_id: str = ""
    disk_provisioning: str = ""
    folder_id: str = ""
    confirm: bool = False


class DiskConversionPlanRequest(BaseModel):
    vm_id: str
    target: str = "thin"
    method: str = "auto"


class DiskConversionRequest(DiskConversionPlanRequest):
    plan_token: str
    confirm: bool = False


class DiskConversionPlan(BaseModel):
    vm_id: str
    vm_name: str
    target: str
    method: str
    fallback_method: str = ""
    plan_token: str
    power_state: str
    disks: List[VirtualDiskSummary] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    estimated_scratch_bytes: int = 0
    noop: bool = False
    can_execute: bool = False


class HostServiceSummary(BaseModel):
    key: str
    label: str = ""
    running: bool = False
    policy: str = ""
    required: bool = False
    controllable: bool = False


class HostStorageAdapterSummary(BaseModel):
    key: str
    model: str = ""
    driver: str = ""
    status: str = ""
    device: str = ""


class HostHealthSensor(BaseModel):
    name: str
    status: str = "unknown"
    reading: str = ""


class HostManagementInfo(BaseModel):
    host_id: str
    name: str
    endpoint_kind: str = ""
    product_name: str = ""
    version: str = ""
    build: str = ""
    api_version: str = ""
    vendor: str = ""
    model: str = ""
    uuid: str = ""
    connection_state: str = "unknown"
    maintenance_mode: bool = False
    uptime_seconds: int = 0
    boot_time: Optional[datetime] = None
    current_time: Optional[datetime] = None
    ntp_servers: List[str] = Field(default_factory=list)
    dns_servers: List[str] = Field(default_factory=list)
    search_domains: List[str] = Field(default_factory=list)
    hostname: str = ""
    domain_name: str = ""
    license_name: str = ""
    license_key: str = ""
    services: List[HostServiceSummary] = Field(default_factory=list)
    storage_adapters: List[HostStorageAdapterSummary] = Field(default_factory=list)
    health: List[HostHealthSensor] = Field(default_factory=list)
    ssh_configured: bool = False
    capabilities: Dict[str, bool] = Field(default_factory=dict)


class HostActionRequest(BaseModel):
    action: str
    confirm: bool = False
    timeout_seconds: int = Field(default=900, ge=30, le=7200)


class HostServiceActionRequest(BaseModel):
    service_key: str
    action: str
    policy: str = ""
    confirm: bool = False


class HostTimeRequest(BaseModel):
    ntp_servers: List[str] = Field(default_factory=list)
    sync_now: bool = False
    confirm: bool = False


class CloneMigrateRequest(BaseModel):
    vm_id: str
    host_id: str
    name: str = ""
    destroy_source: bool = False
    disable_drs: bool = False
    datastore_id: str = ""
    folder_id: str = ""
    power_on: bool = False
    confirm: bool = False


class DrsOverrideRequest(BaseModel):
    vm_ids: List[str]
    confirm: bool = False


class RenameVmRequest(BaseModel):
    name: str
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
