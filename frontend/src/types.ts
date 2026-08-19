export type PowerState = "POWERED_ON" | "POWERED_OFF" | "SUSPENDED" | string;

export type ConnectionInfo = {
  mode: string;
  connected: boolean;
  host: string;
  user: string;
  message: string;
  last_sync: string | null;
  source: string;
  env_ready: boolean;
  has_saved_password: boolean;
  saved_host: string;
  saved_user: string;
  saved_port: number;
  insecure: boolean;
  can_disconnect: boolean;
  stale: boolean;
  syncing: boolean;
  last_error: string;
  queued_jobs: number;
  active_jobs: number;
  cache_age_seconds: number | null;
};

export type HostSummary = {
  id: string;
  name: string;
  cluster_id: string;
  cluster_name: string;
  connection_state: string;
  power_state: string;
  cpu_cores: number;
  cpu_mhz: number;
  cpu_usage_mhz: number;
  cpu_usage_pct: number;
  memory_mib: number;
  memory_usage_mib: number;
  memory_usage_pct: number;
  vm_count: number;
};

export type ClusterSummary = {
  id: string;
  name: string;
  host_count: number;
  vm_count: number;
  cpu_cores: number;
  cpu_usage_mhz: number;
  cpu_capacity_mhz: number;
  cpu_usage_pct: number;
  memory_mib: number;
  memory_usage_mib: number;
  memory_usage_pct: number;
};

export type VirtualMachine = {
  id: string;
  name: string;
  power_state: PowerState;
  cpu_count: number;
  memory_mib: number;
  cpu_usage_mhz: number;
  cpu_usage_pct: number;
  memory_usage_mib: number;
  memory_usage_pct: number;
  host_id: string;
  host_name: string;
  cluster_id: string;
  cluster_name: string;
  guest_os: string;
  tools_status: string;
  ip_address: string | null;
  boot_time: string | null;
  last_activity: string | null;
  last_activity_source: string;
  days_idle: number | null;
  owner_key: string;
  owner_source: string;
  custom_fields: Record<string, string>;
  idle_score: number;
  reclaim_reason: string | null;
  annotation: string;
  storage_used_bytes: number;
  storage_provisioned_bytes: number;
  disk_provisioning: "thin" | "thick" | "mixed" | "unknown" | string;
};

export type OwnerReport = {
  owner_key: string;
  owner_source: string;
  vm_count: number;
  powered_on: number;
  powered_off: number;
  suspended: number;
  cpu_count: number;
  memory_mib: number;
  cpu_usage_mhz: number;
  memory_usage_mib: number;
  idle_candidates: number;
  reclaimable_memory_mib: number;
  vms: string[];
};

export type InventorySnapshot = {
  connection: ConnectionInfo;
  clusters: ClusterSummary[];
  hosts: HostSummary[];
  vms: VirtualMachine[];
  owners: OwnerReport[];
};

export type ActionName = "start" | "shutdown" | "power_off" | "reboot" | "reset" | "suspend" | "destroy";

export type ActionResult = {
  vm_id: string;
  name: string;
  action: string;
  ok: boolean;
  message: string;
};

export type DatastoreSummary = {
  id: string;
  name: string;
  type: string;
  capacity_bytes: number;
  free_bytes: number;
  accessible: boolean;
  datacenter: string;
  datacenter_path: string;
  host_count: number;
  host_ids: string[];
  usage_pct: number;
};

export type NetworkSummary = {
  id: string;
  name: string;
  type: string;
  accessible: boolean;
  host_count: number;
  host_ids: string[];
};

export type DatastoreFile = {
  name: string;
  path: string;
  size: number;
  is_directory: boolean;
  modified: string | null;
  kind: string;
};

export type DatastoreListing = {
  datastore_id: string;
  datastore_name: string;
  path: string;
  files: DatastoreFile[];
  stale: boolean;
};

export type VmTemplate = {
  id: string;
  name: string;
  cpu_count: number;
  memory_mib: number;
  guest_os: string;
  cluster_id: string;
  cluster_name: string;
  datastore_id: string;
  datastore_name: string;
};

export type Catalog = {
  datastores: DatastoreSummary[];
  templates: VmTemplate[];
  networks: NetworkSummary[];
  stale: boolean;
  last_sync: string | null;
};

export type Job = {
  id: string;
  kind: string;
  title: string;
  status: string;
  payload: Record<string, unknown>;
  progress: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string;
  attempts: number;
  max_attempts: number;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
  idempotency_key: string;
};

export type JobList = {
  jobs: Job[];
  queued: number;
  active: number;
};

export type MetricPoint = {
  ts: string;
  cpu_pct: number;
  memory_pct: number;
  disk_pct: number;
  cpu_usage_mhz: number;
  memory_usage_mib: number;
  storage_bytes: number;
};

export type OwnerUtilization = {
  owner_key: string;
  vm_count: number;
  cpu_usage_mhz: number;
  memory_usage_mib: number;
  storage_bytes: number;
  cpu_share_pct: number;
  memory_share_pct: number;
  storage_share_pct: number;
  cpu_usage_pct: number;
  memory_usage_pct: number;
};

export type MetricsResponse = {
  series: MetricPoint[];
  owners: OwnerUtilization[];
  hours: number;
  owner: string;
  host: string;
  datastore: string;
  points: number;
};

export type StagingSession = {
  id: string;
  filename: string;
  size: number;
  received: number;
  complete: boolean;
};

export type PrivilegeCheck = {
  id: string;
  label: string;
  granted: boolean;
};

export type MigrationAccessStatus = {
  principal: string;
  scope: string;
  cluster_id: string;
  cluster_name: string;
  role_name: string;
  can_modify_permissions: boolean;
  privileges: PrivilegeCheck[];
  message: string;
};
