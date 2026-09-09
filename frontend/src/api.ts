import type {
  ActionName,
  ActionResult,
  AutomationCredential,
  AutomationCredentialList,
  Catalog,
  ConnectionInfo,
  ConnectionProfileList,
  ConsoleTicket,
  DatastoreListing,
  DiskConversionPlan,
  HostManagementInfo,
  InventorySnapshot,
  Job,
  JobList,
  MetricsResponse,
  MigrationAccessStatus,
  StagingSession,
  StorageCleanupResponse,
  StorageReconciliationReport,
  ToolsDeploymentResponse,
  ToolsDeploymentTarget,
  VmFolder,
  VmHardwarePlan,
  VmHardwareSpec,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function tokenHeader(): Record<string, string> {
  const token = localStorage.getItem("vfleet.uiToken") || "";
  return token ? { "X-UI-Token": token } : {};
}

async function readError(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const payload = JSON.parse(text) as { detail?: string | Array<{ msg: string }> };
    if (typeof payload.detail === "string") return sanitizeErrorText(payload.detail);
    if (Array.isArray(payload.detail) && payload.detail[0]?.msg) return sanitizeErrorText(payload.detail[0].msg);
  } catch {
    /* keep raw text */
  }
  return sanitizeErrorText(text) || `Request failed (${response.status})`;
}

function sanitizeErrorText(raw: string): string {
  const text = raw.trim();
  if (!text) return text;
  const lower = text.toLowerCase();
  if (lower.includes("<html") || lower.includes("<!doctype") || lower.includes("nginx/")) {
    if (/\b404\b/.test(text) || lower.includes("404 not found")) {
      return "vCenter returned HTTP 404 for the API path (the UI may still work). Check host/port or retry.";
    }
    const title = text.match(/<title>([^<]+)<\/title>/i)?.[1]?.replace(/\s+/g, " ").trim();
    return title ? `vCenter returned an HTML error (${title})` : "vCenter returned an HTML error page instead of the API";
  }
  if (lower.includes("session is not authenticated") || lower.includes("notauthenticated")) {
    return "vCenter API session expired. Reconnecting; use Connect if inventory stays stale.";
  }
  const soapMsg = text.match(/\bmsg\s*=\s*'([^']+)'/i);
  if (soapMsg && (lower.includes("vim.fault") || lower.includes("vmodl."))) {
    return soapMsg[1].replace(/\.\s*$/, "");
  }
  return text.length > 240 ? `${text.slice(0, 237)}…` : text;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  Object.entries(tokenHeader()).forEach(([key, value]) => headers.set(key, value));
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) throw new ApiError(response.status, await readError(response));
  return response.json() as Promise<T>;
}

export async function fetchInventory(params: Record<string, string> = {}): Promise<InventorySnapshot> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) query.set(key, value);
  }
  const suffix = query.toString() ? `?${query}` : "";
  return request<InventorySnapshot>(`/api/inventory${suffix}`);
}

export async function fetchConnection(): Promise<ConnectionInfo> {
  return request<ConnectionInfo>("/api/connection");
}

export async function fetchConnectionProfiles(): Promise<ConnectionProfileList> {
  return request<ConnectionProfileList>("/api/connection-profiles");
}

export async function connectProfile(profileId: string): Promise<ConnectionInfo> {
  return request<ConnectionInfo>(`/api/connection-profiles/${encodeURIComponent(profileId)}/connect`, { method: "POST" });
}

export async function deleteConnectionProfile(profileId: string): Promise<void> {
  await request<{ deleted: boolean }>(`/api/connection-profiles/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
    body: JSON.stringify({ confirm: true }),
  });
}

export async function login(body: {
  host: string;
  user: string;
  password: string;
  port: number;
  insecure: boolean;
  remember: boolean;
  connect: boolean;
  endpoint_kind?: string;
  ssh_start_service_confirm?: boolean;
  ssh_enabled?: boolean;
  ssh_user?: string;
  ssh_password?: string;
  ssh_port?: number;
  ssh_host_key_sha256?: string;
  jump_enabled?: boolean;
  jump_address?: string;
  jump_port?: number;
  jump_host_type?: "auto" | "windows" | "unix";
  jump_credential_id?: string;
  jump_host_key_sha256?: string;
  profile_id?: string;
  profile_name?: string;
}): Promise<ConnectionInfo> {
  return request<ConnectionInfo>("/api/login", { method: "POST", body: JSON.stringify(body) });
}

export async function logout(forget = false): Promise<ConnectionInfo> {
  return request<ConnectionInfo>("/api/logout", { method: "POST", body: JSON.stringify({ forget }) });
}

export async function runActions(vmIds: string[], action: ActionName): Promise<ActionResult[]> {
  const payload = await request<{ results: ActionResult[]; job_id?: string; queued?: boolean }>("/api/actions", {
    method: "POST",
    body: JSON.stringify({ vm_ids: vmIds, action, confirm: true }),
  });
  return payload.results;
}

export async function fetchAutomationCredentials(options: { profileId?: string; globalOnly?: boolean } = {}): Promise<AutomationCredentialList> {
  const query = new URLSearchParams();
  if (options.profileId) query.set("profile_id", options.profileId);
  if (options.globalOnly) query.set("global_only", "true");
  const suffix = query.toString() ? `?${query}` : "";
  return request<AutomationCredentialList>(`/api/automation-credentials${suffix}`);
}

export async function saveAutomationCredential(body: {
  id?: string;
  name: string;
  kind: "windows" | "ssh" | "service";
  username: string;
  secret: string;
  scope: "global" | "endpoint";
}): Promise<AutomationCredential> {
  return request<AutomationCredential>("/api/automation-credentials", {
    method: "POST",
    body: JSON.stringify({ ...body, confirm: true }),
  });
}

export async function deleteAutomationCredential(id: string): Promise<void> {
  await request<{ deleted: boolean }>(`/api/automation-credentials/${encodeURIComponent(id)}`, {
    method: "DELETE",
    body: JSON.stringify({ confirm: true }),
  });
}

export async function fetchSshHostKey(address: string, port = 22, jump?: {
  address: string;
  port: number;
  credential_id: string;
  host_key_sha256: string;
}): Promise<string> {
  const query = new URLSearchParams({ address, port: String(port) });
  if (jump) {
    query.set("jump_address", jump.address);
    query.set("jump_port", String(jump.port));
    query.set("jump_credential_id", jump.credential_id);
    query.set("jump_host_key_sha256", jump.host_key_sha256);
  }
  const result = await request<{ fingerprint: string }>(`/api/tools/ssh-host-key?${query}`);
  return result.fingerprint;
}

export async function testSshConnection(body: {
  address: string;
  port: number;
  credential_id: string;
  host_key_sha256: string;
  host_type?: "auto" | "windows" | "unix";
  profile_id?: string;
}): Promise<{ host_type: "auto" | "windows" | "unix"; detected_host_type: "auto" | "windows" | "unix" }> {
  return request<{ ok: boolean; host_type: "auto" | "windows" | "unix"; detected_host_type: "auto" | "windows" | "unix" }>("/api/tools/ssh-connection-test", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deployGuestTools(body: {
  targets: ToolsDeploymentTarget[];
  credential_id: string;
  windows_transport: "http" | "https";
  windows_port: number;
  validate_certificate: boolean;
  linux_port: number;
  sudo: boolean;
  jump_address: string;
  jump_port: number;
  jump_host_type: "auto" | "windows" | "unix";
  jump_credential_id: string;
  jump_host_key_sha256: string;
}): Promise<ToolsDeploymentResponse> {
  return request<ToolsDeploymentResponse>("/api/tools/deploy", {
    method: "POST",
    body: JSON.stringify({ ...body, confirm: true }),
  });
}

export async function preflightGuestTools(body: {
  targets: ToolsDeploymentTarget[];
  credential_id: string;
  windows_transport: "http" | "https";
  windows_port: number;
  validate_certificate: boolean;
  linux_port: number;
  sudo: boolean;
  jump_address: string;
  jump_port: number;
  jump_host_type: "auto" | "windows" | "unix";
  jump_credential_id: string;
  jump_host_key_sha256: string;
}): Promise<{
  ok: boolean;
  vm_id: string;
  vm_name: string;
  os_family: string;
  details: Record<string, unknown>;
}> {
  return request("/api/tools/preflight", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function fetchConsoleTicket(vmId: string, type: "vmrc" | "webmks" = "vmrc"): Promise<ConsoleTicket> {
  return request<ConsoleTicket>(`/api/vms/${encodeURIComponent(vmId)}/console`, {
    method: "POST",
    body: JSON.stringify({ type }),
  });
}

export function vcenterVmConsoleUrl(host: string, vmId: string, port = 443): string {
  const authority = !host || port === 443 ? host : `${host}:${port}`;
  return `https://${authority}/ui/app/vm;nav=s/urn:vmomi:VirtualMachine:${vmId}/console`;
}

export const VMRC_INSTALL_URL =
  "https://support.broadcom.com/group/ecx/productdownloads?subfamily=VMware%20Remote%20Console";

/** Launch vmrc:// and other custom protocol URIs without leaving a blank browser tab. */
export function launchExternalUri(uri: string): void {
  if (/^[a-z][a-z0-9+.-]*:/i.test(uri) && !/^https?:/i.test(uri)) {
    const link = document.createElement("a");
    link.href = uri;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    link.remove();
    return;
  }
  window.open(uri, "_blank", "noopener,noreferrer");
}

export async function fetchCatalog(): Promise<Catalog> {
  return request<Catalog>("/api/catalog");
}

export async function listVmFolders(datacenter = ""): Promise<VmFolder[]> {
  const query = datacenter ? `?datacenter=${encodeURIComponent(datacenter)}` : "";
  return request<VmFolder[]>(`/api/folders${query}`);
}

export async function fetchMetrics(
  params: { hours?: number; owner?: string; host?: string; datastore?: string; since?: string; until?: string } = {},
): Promise<MetricsResponse> {
  const query = new URLSearchParams();
  if (params.hours !== undefined) query.set("hours", String(params.hours));
  if (params.owner) query.set("owner", params.owner);
  if (params.host) query.set("host", params.host);
  if (params.datastore) query.set("datastore", params.datastore);
  if (params.since) query.set("since", params.since);
  if (params.until) query.set("until", params.until);
  const suffix = query.toString() ? `?${query}` : "";
  return request<MetricsResponse>(`/api/metrics${suffix}`);
}

export async function fetchDatastoreFiles(datastoreId: string, path = ""): Promise<DatastoreListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return request<DatastoreListing>(`/api/datastores/${encodeURIComponent(datastoreId)}/files${query}`);
}

export async function fetchJobs(): Promise<JobList> {
  return request<JobList>("/api/jobs");
}

export async function cancelJob(jobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/cancel`, { method: "POST" });
}

export async function retryJob(jobId: string): Promise<Job> {
  return request<Job>(`/api/jobs/${jobId}/retry`, { method: "POST" });
}

export async function deleteJob(jobId: string): Promise<void> {
  await request<{ deleted: boolean }>(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
}

export async function clearJobHistory(otherEndpoints = false): Promise<number> {
  const result = await request<{ deleted: number }>("/api/jobs/history", {
    method: "DELETE",
    body: JSON.stringify({ confirm: true, other_endpoints: otherEndpoints }),
  });
  return result.deleted;
}

export async function fetchMigrationAccess(clusterId = ""): Promise<MigrationAccessStatus> {
  const suffix = clusterId ? `?cluster_id=${encodeURIComponent(clusterId)}` : "";
  return request<MigrationAccessStatus>(`/api/migration-access${suffix}`);
}

export async function grantMigrationAccess(body: {
  confirm: boolean;
  scope: "cluster" | "global";
  cluster_id?: string;
  job_id?: string;
}): Promise<MigrationAccessStatus> {
  return request<MigrationAccessStatus>("/api/migration-access", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function cloneVm(body: {
  template_id: string;
  name: string;
  datastore_id: string;
  cluster_id?: string;
  host_id?: string;
  folder_id?: string;
  disable_drs?: boolean;
  cpu_count?: number;
  memory_mib?: number;
  power_on?: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms", { method: "POST", body: JSON.stringify(body) });
}

export async function deployFromTemplate(body: {
  template_id: string;
  name: string;
  datastore_id: string;
  cluster_id?: string;
  host_id?: string;
  folder_id?: string;
  disable_drs?: boolean;
  cpu_count?: number;
  memory_mib?: number;
  power_on?: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms/deploy", { method: "POST", body: JSON.stringify(body) });
}

export async function renameVm(vmId: string, name: string): Promise<Job> {
  return request<Job>(`/api/vms/${encodeURIComponent(vmId)}/rename`, {
    method: "POST",
    body: JSON.stringify({ name, confirm: true }),
  });
}

export async function applyDrsOverride(vmIds: string[]): Promise<Job> {
  return request<Job>("/api/vms/drs-override", {
    method: "POST",
    body: JSON.stringify({ vm_ids: vmIds, confirm: true }),
  });
}

export async function planVmHardware(body: VmHardwareSpec): Promise<VmHardwarePlan> {
  return request<VmHardwarePlan>("/api/vms/hardware/plan", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function configureVmHardware(body: VmHardwareSpec, planToken: string): Promise<Job> {
  return request<Job>("/api/vms/hardware", {
    method: "POST",
    body: JSON.stringify({ ...body, plan_token: planToken, confirm: true }),
  });
}

export async function migrateVms(body: {
  vm_ids: string[];
  host_id?: string;
  datastore_id?: string;
  network_id?: string;
  disk_provisioning?: string;
  folder_id?: string;
  confirm: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms/migrate", { method: "POST", body: JSON.stringify(body) });
}

export async function planDiskConversion(body: {
  vm_id: string;
  target: string;
  method: string;
}): Promise<DiskConversionPlan> {
  return request<DiskConversionPlan>("/api/vms/disk-conversion/plan", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function convertVmDisks(plan: DiskConversionPlan, reconcileAfter = true): Promise<Job> {
  return request<Job>("/api/vms/disk-conversion", {
    method: "POST",
    body: JSON.stringify({
      vm_id: plan.vm_id,
      target: plan.target,
      method: plan.method,
      plan_token: plan.plan_token,
      reconcile_after: reconcileAfter,
      confirm: true,
    }),
  });
}

export async function planStorageReconciliation(body: {
  vm_ids: string[];
  include_unregistered_directories: boolean;
}): Promise<StorageReconciliationReport> {
  return request<StorageReconciliationReport>("/api/storage/reconciliation", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function cleanupStorageReconciliation(body: {
  vm_ids: string[];
  include_unregistered_directories: boolean;
  candidate_ids: string[];
  manual_validated_candidate_ids: string[];
  plan_token: string;
}): Promise<StorageCleanupResponse> {
  return request<StorageCleanupResponse>("/api/storage/reconciliation/cleanup", {
    method: "POST",
    body: JSON.stringify({ ...body, confirm: true }),
  });
}

export async function fetchHostManagement(): Promise<HostManagementInfo> {
  return request<HostManagementInfo>("/api/host");
}

export async function queueHostAction(action: string): Promise<Job> {
  return request<Job>("/api/host/actions", {
    method: "POST",
    body: JSON.stringify({ action, confirm: true, timeout_seconds: 900 }),
  });
}

export async function queueHostService(service_key: string, action: string, policy = ""): Promise<Job> {
  return request<Job>("/api/host/services", {
    method: "POST",
    body: JSON.stringify({ service_key, action, policy, confirm: true }),
  });
}

export async function queueHostTime(ntp_servers: string[], sync_now: boolean): Promise<Job> {
  return request<Job>("/api/host/time", {
    method: "POST",
    body: JSON.stringify({ ntp_servers, sync_now, confirm: true }),
  });
}

export async function queueStorageRescan(): Promise<Job> {
  return request<Job>("/api/host/storage/rescan", {
    method: "POST",
    body: JSON.stringify({ action: "rescan", confirm: true }),
  });
}

export async function queueSupportBundle(): Promise<Job> {
  return request<Job>("/api/host/support-bundle", {
    method: "POST",
    body: JSON.stringify({ action: "support_bundle", confirm: true }),
  });
}

export async function cloneMigrate(body: {
  vm_id: string;
  host_id: string;
  name?: string;
  destroy_source?: boolean;
  disable_drs?: boolean;
  datastore_id?: string;
  folder_id?: string;
  power_on?: boolean;
  confirm: boolean;
}): Promise<Job> {
  return request<Job>("/api/vms/clone-migrate", { method: "POST", body: JSON.stringify(body) });
}

export async function mkdir(datastoreId: string, path: string): Promise<Job> {
  return request<Job>("/api/datastores/mkdir", {
    method: "POST",
    body: JSON.stringify({ datastore_id: datastoreId, path }),
  });
}

export async function deleteDatastoreFile(datastoreId: string, path: string): Promise<Job> {
  return request<Job>("/api/datastores/delete", {
    method: "POST",
    body: JSON.stringify({ datastore_id: datastoreId, path, confirm: true }),
  });
}

export async function createStaging(filename: string, size: number): Promise<StagingSession> {
  return request<StagingSession>("/api/staging", {
    method: "POST",
    body: JSON.stringify({ filename, size }),
  });
}

export async function putStagingChunk(id: string, blob: Blob, start: number, total: number): Promise<StagingSession> {
  const headers = new Headers(tokenHeader());
  const end = start + blob.size - 1;
  headers.set("Content-Range", `bytes ${start}-${end}/${total}`);
  headers.set("Content-Type", "application/octet-stream");
  const response = await fetch(`/api/staging/${id}`, { method: "PUT", headers, body: blob });
  if (!response.ok) throw new ApiError(response.status, await readError(response));
  return response.json() as Promise<StagingSession>;
}

export async function enqueueUpload(body: {
  staging_id: string;
  datastore_id: string;
  remote_path: string;
  use_library: boolean;
}): Promise<Job> {
  return request<Job>("/api/uploads", { method: "POST", body: JSON.stringify(body) });
}

export function setUiToken(token: string) {
  if (token) localStorage.setItem("vfleet.uiToken", token);
  else localStorage.removeItem("vfleet.uiToken");
}

export type HostUpgradePlan = {
  staging_id: string;
  reserved?: boolean;
  startup_order?: string[];
  id: string;
  phase: string;
  created_at: string;
  media: {
    version: string; build: string; sha256: string; size: number;
    method?: string; free_edition?: boolean; installer_complete?: boolean;
    boot_module_count?: number; upgrade_metadata_present?: boolean;
    image_profile?: string; automation_mode?: string;
  };
  context: {
    host: { version: string; build: string; name: string };
    vms: Array<{ uuid: string; name: string; power_state: string }>;
  };
  blockers: string[];
  notice: string;
  sources: Array<{ title: string; url: string }>;
  shutdown_completed: string[];
  restored: string[];
};

export const inspectHostUpgrade = (staging_id: string) => request<Job>("/api/host/upgrade/inspect", {
  method: "POST", body: JSON.stringify({ staging_id }),
});
export const fetchHostUpgrade = (id: string) => request<HostUpgradePlan>(`/api/host/upgrade/plans/${encodeURIComponent(id)}`);
export const fetchActiveHostUpgrade = () => request<{ plan: HostUpgradePlan | null }>("/api/host/upgrade/active");
export const fetchUpgradeJob = (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}`);

export type UpgradeUsbDevice = {
  number: number;
  friendly_name: string;
  serial_number: string;
  unique_id: string;
  size_bytes: number;
  drive_letters: string[];
  safe: boolean;
  blocked_reason: string;
};
export type UpgradeUsbInventory = {
  supported: boolean;
  platform: string;
  devices: UpgradeUsbDevice[];
  message: string;
};
export const fetchUpgradeUsbDevices = () => request<UpgradeUsbInventory>("/api/host/upgrade/usb");
export const writeUpgradeUsb = (body: {
  plan_id: string;
  disk_number: number;
  friendly_name: string;
  serial_number: string;
  unique_id: string;
  size_bytes: number;
  confirmation: string;
}) => request<Job>("/api/host/upgrade/usb/write", { method: "POST", body: JSON.stringify(body) });

export const prepareHostUpgrade = (body: {
  plan_id: string; confirm: boolean; path_verified: boolean; hardware_verified: boolean;
  vm_backups_verified: boolean; independent_controller: boolean; installer_ready: boolean; manage_ssh_service: boolean;
  publisher_sha256: string; startup_order: string[]; shutdown_timeout_seconds: number; startup_delay_seconds: number;
}) => request<Job>("/api/host/upgrade/prepare", { method: "POST", body: JSON.stringify(body) });
export const recoverHostUpgrade = (plan_id: string, mode: "complete" | "abort") => request<Job>("/api/host/upgrade/recover", {
  method: "POST", body: JSON.stringify({ plan_id, mode, confirm: true }),
});
