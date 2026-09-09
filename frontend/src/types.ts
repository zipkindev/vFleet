export type PowerState = "POWERED_ON" | "POWERED_OFF" | "SUSPENDED" | string;
export type JumpHostType = "auto" | "windows" | "unix";

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
  endpoint_kind: "demo" | "vcenter" | "esxi" | string;
  api_type: string;
  api_version: string;
  product_name: string;
  product_version: string;
  product_build: string;
  instance_uuid: string;
  endpoint_fingerprint: string;
  capabilities: Record<string, boolean>;
  ssh_configured: boolean;
  ssh_user: string;
  ssh_port: number;
  ssh_host_key_sha256: string;
  has_saved_ssh_password: boolean;
  active_profile_id: string;
};

export type ConnectionProfile = {
  id: string;
  name: string;
  host: string;
  user: string;
  port: number;
  insecure: boolean;
  endpoint_kind: "auto" | "vcenter" | "esxi" | string;
  endpoint_fingerprint: string;
  ssh_enabled: boolean;
  ssh_user: string;
  ssh_port: number;
  ssh_host_key_sha256: string;
  has_saved_password: boolean;
  has_saved_ssh_password: boolean;
  jump_enabled: boolean;
  jump_address: string;
  jump_port: number;
  jump_host_type: JumpHostType;
  jump_credential_id: string;
  jump_host_key_sha256: string;
  active: boolean;
  last_used_at: string | null;
};

export type ConnectionProfileList = {
  profiles: ConnectionProfile[];
  active_profile_id: string;
  credential_storage: string;
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
  cores_per_socket: number;
  cpu_hot_add_enabled: boolean;
  memory_hot_add_enabled: boolean;
  memory_usage_mib: number;
  memory_usage_pct: number;
  vm_count: number;
  maintenance_mode: boolean;
  uptime_seconds: number;
  vendor: string;
  model: string;
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
  host_memory_usage_mib: number;
  ballooned_memory_mib: number;
  swapped_memory_mib: number;
  compressed_memory_mib: number;
  host_id: string;
  host_name: string;
  cluster_id: string;
  cluster_name: string;
  guest_os: string;
  tools_status: string;
  tools_installer_mounted: boolean;
  ip_address: string | null;
  mac_addresses: string[];
  boot_time: string | null;
  last_activity: string | null;
  last_activity_source: string;
  days_idle: number | null;
  owner_key: string;
  owner_source: string;
  deployed_by: string;
  custom_fields: Record<string, string>;
  idle_score: number;
  reclaim_reason: string | null;
  annotation: string;
  storage_used_bytes: number;
  storage_provisioned_bytes: number;
  disk_provisioning: "thin" | "thick" | "mixed" | "unknown" | string;
  drs_override: boolean;
  folder_id?: string;
  folder_path?: string;
  disks: VirtualDiskSummary[];
  cdrom_count: number;
  mounted_iso_path: string;
  iso_connected: boolean;
  iso_start_connected: boolean;
};

export type VirtualDiskSummary = {
  key: number;
  label: string;
  capacity_bytes: number;
  file_name: string;
  datastore_id: string;
  datastore_name: string;
  provisioning: string;
  disk_mode: string;
  backing_type: string;
  parent_depth: number;
  rdm: boolean;
  encrypted: boolean;
  sharing: string;
};

export type DiskConversionPlan = {
  vm_id: string;
  vm_name: string;
  target: string;
  method: "soap" | "ssh" | string;
  fallback_method: string;
  plan_token: string;
  power_state: string;
  disks: VirtualDiskSummary[];
  blockers: string[];
  warnings: string[];
  estimated_scratch_bytes: number;
  noop: boolean;
  can_execute: boolean;
};

export type VmHardwareSpec = {
  vm_ids: string[];
  cpu_count?: number;
  memory_mib?: number;
  disk_index?: number;
  disk_capacity_bytes?: number;
  iso_action: "keep" | "mount" | "eject";
  iso_datastore_id?: string;
  iso_path?: string;
  iso_connect_at_power_on?: boolean;
  shutdown_before?: boolean;
  force_power_off_on_timeout?: boolean;
  power_on_after?: boolean;
  shutdown_timeout_seconds?: number;
};

export type VmHardwareTargetPlan = {
  vm_id: string;
  name: string;
  cluster_id: string;
  cluster_name: string;
  host_id: string;
  host_name: string;
  power_state: string;
  current_cpu_count: number;
  target_cpu_count: number | null;
  current_memory_mib: number;
  target_memory_mib: number | null;
  disk_index: number | null;
  disk_label: string;
  current_disk_capacity_bytes: number;
  target_disk_capacity_bytes: number | null;
  current_iso_path: string;
  target_iso_path: string;
  changes: string[];
  blockers: string[];
  warnings: string[];
  will_shutdown_before: boolean;
  may_force_power_off: boolean;
  will_power_on_after: boolean;
  noop: boolean;
  can_execute: boolean;
};

export type VmHardwarePlan = {
  plan_token: string;
  targets: VmHardwareTargetPlan[];
  can_execute_count: number;
  blocked_count: number;
  noop_count: number;
};

export type StorageReconciliationCandidate = {
  id: string;
  kind: "preserved_source_disk" | "unattached_virtual_disk" | "unregistered_vm_directory";
  datastore_id: string;
  datastore_name: string;
  path: string;
  size: number;
  vm_id: string;
  vm_name: string;
  job_id: string;
  confidence: "high" | "review";
  validation_status: "automated" | "manual_required" | "blocked";
  can_delete: boolean;
  reason: string;
  warning: string;
};

export type StorageReconciliationVmStatus = {
  vm_id: string;
  vm_name: string;
  power_state: string;
  tools_status: string;
  tools_running: boolean;
  boot_validated: boolean;
  manual_validation_required: boolean;
  message: string;
};

export type StorageReconciliationReport = {
  plan_token: string;
  scanned_at: string;
  inventory_stale: boolean;
  vm_ids: string[];
  scanned_directories: number;
  candidates: StorageReconciliationCandidate[];
  vm_statuses: StorageReconciliationVmStatus[];
  warnings: string[];
};

export type StorageCleanupResponse = {
  jobs: Job[];
  failures: string[];
};

export type HostServiceSummary = {
  key: string;
  label: string;
  running: boolean;
  policy: string;
  required: boolean;
  controllable: boolean;
};

export type HostManagementInfo = {
  host_id: string;
  name: string;
  endpoint_kind: string;
  product_name: string;
  version: string;
  build: string;
  api_version: string;
  vendor: string;
  model: string;
  uuid: string;
  connection_state: string;
  maintenance_mode: boolean;
  uptime_seconds: number;
  boot_time: string | null;
  current_time: string | null;
  ntp_servers: string[];
  dns_servers: string[];
  search_domains: string[];
  hostname: string;
  domain_name: string;
  license_name: string;
  license_key: string;
  services: HostServiceSummary[];
  storage_adapters: Array<{ key: string; model: string; driver: string; status: string; device: string }>;
  health: Array<{ name: string; status: string; reading: string }>;
  ssh_configured: boolean;
  capabilities: Record<string, boolean>;
};

export type VmFolder = {
  id: string;
  name: string;
  path: string;
  parent_id: string;
  datacenter_id: string;
  datacenter_name: string;
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

export type ActionName = "start" | "shutdown" | "power_off" | "reboot" | "reset" | "suspend" | "mount_tools" | "destroy";

export type AutomationCredential = {
  id: string;
  name: string;
  kind: "windows" | "ssh" | "service";
  username: string;
  scope: "global" | "endpoint";
  endpoint_fingerprint: string;
  has_secret: boolean;
  created_at: string;
  updated_at: string;
};

export type AutomationCredentialList = {
  credentials: AutomationCredential[];
  storage: string;
};

export type ToolsDeploymentTarget = {
  vm_id: string;
  address: string;
  os_family: "auto" | "windows" | "linux" | "pfsense";
  ssh_host_key_sha256?: string;
};

export type ToolsDeploymentResponse = {
  jobs: Job[];
  failures: string[];
};

export type ActionResult = {
  vm_id: string;
  name: string;
  action: string;
  ok: boolean;
  message: string;
};

export type ConsoleTicket = {
  vm_id: string;
  name: string;
  type: string;
  uri: string;
  host: string;
  port: number;
  ticket: string;
  ssl_thumbprint: string;
  vcenter_url: string;
  expires_in_seconds: number;
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
  readonly?: boolean;
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
  endpoint_fingerprint: string;
  hidden_other_endpoints: number;
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
